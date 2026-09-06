"""Re-translate a finished sentence as a whole.

The live translation the server produces is a *streaming prefix commit*: it
freezes the first few Chinese words before the English sentence is finished, so
word order and word choice are locked in before the meaning is known. Worse,
NLLB-1.3B emits an end-of-sequence early on compound sentences and silently
drops whole clauses.

Once the ASR has moved on to the next sentence the previous one is final and can
be translated properly -- as one unit, with the previous sentence as context and
the user's glossary in the prompt. That takes a local instruction-following
model rather than a sentence-pair translation model.

The model loads in a background thread so a meeting can start immediately;
refinement simply begins working a few sentences in.
"""

import logging
import os
import queue
import threading
import time

from meeting_subtitles.serve import has_snapshot, hf_cache

logger = logging.getLogger(__name__)

# Set before anything can import huggingface_hub in this process (see _load).
# ``setdefault`` on purpose: HF_HUB_OFFLINE=0 in the environment is the one
# supported way to let this process download the model, which is what the
# README's snapshot_download line uses.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
# The Xet transfer backend fails behind many local proxies and buys nothing
# for a load that is meant to be offline anyway.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

DEFAULT_MODEL = "Qwen/Qwen3-4B-Instruct-2507"

#: Weight files count towards the VRAM estimate; the tokenizer and the JSON
#: configs do not, and a stray README would skew a small model badly.
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt")

#: On top of the weights: CUDA context, the KV cache for one short generation,
#: and enough slack that a transient allocation does not tip the card over.
_VRAM_HEADROOM = 1.0 * 1024 ** 3
_VRAM_OVERHEAD = 1.15


def repo_dir(model_id: str) -> str:
    """Hub cache directory name for a repo id, e.g. ``models--Qwen--Qwen3-4B``."""
    return "models--" + model_id.replace("/", "--")


def is_cached(model_id: str) -> bool:
    """Whether the refiner's weights are already on this disk."""
    directory = repo_dir(model_id)
    return (has_snapshot(directory, "*.safetensors")
            or has_snapshot(directory, "*.bin"))


def weights_bytes(model_id: str) -> int:
    """Size of the cached weight files, or 0 when the model is not cached.

    Read through the snapshot links rather than from ``blobs/``: a cache can
    hold revisions nothing points at any more -- a stale ``refs/pr/*`` copy of
    NLLB is 5.5 GB on this machine -- and counting those would over-estimate
    the model by a whole extra copy of itself.
    """
    snapshots = hf_cache() / repo_dir(model_id) / "snapshots"
    if not snapshots.is_dir():
        return 0
    best = 0
    for revision in snapshots.iterdir():
        total = 0
        for item in revision.rglob("*"):
            if item.suffix in _WEIGHT_SUFFIXES:
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
        best = max(best, total)
    return best


SYSTEM_PROMPT = (
    "You are a professional simultaneous interpreter working in a live business "
    "and engineering meeting. Translate the user's English into natural, fluent "
    "Simplified Chinese that a native speaker would actually say.\n"
    "Rules:\n"
    "- Output ONLY the Chinese translation. No explanations, no pinyin, no notes.\n"
    "- Translate the whole input. Never drop a clause.\n"
    "- Keep technical terms, product names and acronyms in English when Chinese "
    "engineers normally say them in English (e.g. Kubernetes, canary rollout, "
    "P99, SLO, pull request).\n"
    "- The input is speech-to-text output: it may contain filler words, missing "
    "punctuation or small recognition errors. Translate the intended meaning.\n"
    "- If the input is not meaningful speech, output it unchanged."
)


#: A leak looks like a *label line* the prompt never asked for -- "术语表：…"
#: or "Previous sentence: …" -- not like a word appearing mid-sentence. Matching
#: bare words was far too aggressive: "上下文" is the correct translation of
#: "context", so every sentence about a context window had its refinement thrown
#: away, which is exactly the vocabulary this user's meetings are full of.
_SCAFFOLD_LABELS = (
    "术语表", "前一句", "系统提示", "上下文（", "上下文(",
    "Glossary", "Previous sentence", "Terms that appear", "Translate into",
)


def _looks_like_scaffolding(text: str) -> bool:
    """True only when a line *is* a prompt label, not merely contains a word."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for label in _SCAFFOLD_LABELS:
            if line.startswith(label):
                rest = line[len(label):]
                if rest == "" or rest[0] in "：:（(":
                    return True
    return False


class TranslationRefiner:
    """Background whole-sentence re-translation, keyed by the English text."""

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        device: str = "cuda",
        glossary: str = "",
        guidance: str = "",
        max_new_tokens: int = 256,
        max_pending: int = 32,
    ) -> None:
        self.model_id = model_id
        self.device = device
        self.glossary = (glossary or "").strip()
        self.guidance = (guidance or "").strip()
        self.max_new_tokens = max_new_tokens
        self.queue: queue.Queue[tuple | None] = queue.Queue(maxsize=max_pending)
        self._results: dict[str, str] = {}
        self._submitted: set = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._model = None
        self._tokenizer = None
        self.ready = threading.Event()
        self.failed: str | None = None
        self.stats = {"count": 0, "total_seconds": 0.0}

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="refiner", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Shut the worker down without killing it mid-load.

        The worker is a daemon thread, so if the process exits while it is
        inside a CUDA allocation the interpreter segfaults on the way out --
        observed when a session error tore the meeting down a few seconds after
        start. Waiting for the load to finish first makes teardown boring.
        """
        if self._worker is None:
            return
        self.ready.wait(timeout=timeout)
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=timeout)
        self._model = None
        self._tokenizer = None

    def _require_vram(self, torch) -> None:
        """Refuse to load when the card cannot hold the model.

        This is not a nicety. The engine is a *separate process* on the same
        GPU, so an out-of-memory here does not fail here: the driver hands the
        shortfall to whichever process allocates next, and that is the ASR,
        which then throws on every single chunk while its WebSocket stays open
        and its transcript file stays readable. The meeting looks alive and
        silently stops producing text. Declining to load costs the polish;
        loading anyway costs the transcript.
        """
        if self.device != "cuda" or not torch.cuda.is_available():
            return
        weights = weights_bytes(self.model_id)
        if not weights:
            return
        needed = weights * _VRAM_OVERHEAD + _VRAM_HEADROOM
        free, total = torch.cuda.mem_get_info()
        if free >= needed:
            return
        gb = 1024 ** 3
        raise RuntimeError(
            f"显存不足：润色模型需要约 {needed / gb:.1f} GB，当前空闲 {free / gb:.1f} GB"
            f"（显卡共 {total / gb:.1f} GB）。\n"
            "先关掉占用显存的其他程序（nvidia-smi 可以看是谁），"
            "或在启动器里关闭「整句润色」。"
        )

    def _load(self) -> None:
        # Order matters: huggingface_hub reads HF_HUB_OFFLINE into a module
        # constant at import time, so setting it after importing transformers
        # does nothing and the load goes to the network -- which on this machine
        # means a black-holed huggingface.co and an indefinite hang. Configure
        # the environment first, import second.
        from meeting_subtitles.envfix import clear_proxy_env

        cached = is_cached(self.model_id)
        if cached:
            # Not setdefault: whatever the desktop, the shell or an env file
            # left behind, a model that is already on disk has nothing to
            # fetch, and one stray HF_HUB_OFFLINE=0 is the difference between
            # a 20 s load and a wait on a hostname that does not resolve.
            os.environ["HF_HUB_OFFLINE"] = "1"
            # Nothing will be fetched, so a proxy can only cause harm: httpx
            # rejects GNOME's socks:// scheme from the *constructor*, which
            # aborts the load while building a client that is never used.
            clear_proxy_env()
        elif os.environ.get("HF_HUB_OFFLINE") != "0":
            raise RuntimeError(
                f"本地没有润色模型 {self.model_id}，且当前是离线模式。\n"
                "先下载一次（需要能连上 huggingface.co）：\n"
                "  HF_HUB_OFFLINE=0 .venv/bin/python -c \"from huggingface_hub import "
                f"snapshot_download; snapshot_download('{self.model_id}')\"\n"
                "或在启动器里关闭「整句润色」。"
            )

        # Imported lazily: a headless or --no-refine run should not pay for
        # transformers' import cost, let alone a model load.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._require_vram(torch)

        logger.info("加载润色模型 %s（%s）…", self.model_id,
                    "离线" if cached else "需要下载")
        started = time.time()
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            dtype=torch.bfloat16,
            device_map=self.device,
        )
        self._model.eval()
        # The first generate() call pays CUDA graph / kernel autotune costs --
        # measured at 3.2 s versus 0.25 s steady state. Spend it here, before
        # any real sentence is waiting on it.
        try:
            self._translate("This is a warm up sentence.", None)
            self.stats["count"] = 0
            self.stats["total_seconds"] = 0.0
        except Exception as exc:
            logger.debug("预热失败（不影响使用）: %s", exc)
        logger.info("润色模型就绪，用时 %.1f 秒", time.time() - started)

    def _run(self) -> None:
        try:
            self._load()
        except Exception as exc:
            self.failed = str(exc)
            logger.warning("润色模型加载失败，将只使用流式译文: %s", exc)
            self.ready.set()
            return
        self.ready.set()

        while True:
            item = self.queue.get()
            if item is None:
                return
            english, previous = item
            try:
                chinese = self._translate(english, previous)
            except Exception as exc:
                logger.warning("整句重译失败: %s", exc)
                continue
            if chinese:
                with self._lock:
                    self._results[english] = chinese

    # ------------------------------------------------------------- inference

    def _build_system(self, previous: str | None) -> str:
        """All instructions live in the system turn.

        Anything placed in the user turn is, to the model, text to translate --
        a glossary or a context line there gets rendered into Chinese and
        returned as if it were the translation.
        """
        parts = [SYSTEM_PROMPT]
        if self.guidance:
            parts.append(self.guidance)
        if self.glossary:
            parts.append(
                "Terms that appear in this meeting, keep them recognisable: "
                f"{self.glossary}"
            )
        if previous:
            parts.append(
                "For context only, the speaker's previous sentence was: "
                f"{previous}"
            )
        return "\n\n".join(parts)

    def _translate(self, english: str, previous: str | None) -> str:
        import torch

        messages = [
            {"role": "system", "content": self._build_system(previous)},
            # The user turn holds nothing but the sentence to translate.
            {"role": "user", "content": english},
        ]
        text = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        inputs = self._tokenizer(text, return_tensors="pt").to(self._model.device)
        started = time.time()
        with torch.inference_mode():
            output = self._model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
                temperature=None,
                top_p=None,
                top_k=None,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        generated = output[0][inputs["input_ids"].shape[1]:]
        result = self._tokenizer.decode(generated, skip_special_tokens=True).strip()
        if _looks_like_scaffolding(result):
            logger.debug("丢弃疑似复述提示词的输出: %s", result[:60])
            return ""

        elapsed = time.time() - started
        self.stats["count"] += 1
        self.stats["total_seconds"] += elapsed
        logger.debug("重译 %.2fs: %s -> %s", elapsed, english[:40], result[:40])
        return result

    # ----------------------------------------------------------------- queue

    def submit(self, english: str, previous: str | None = None) -> None:
        """Queue a finished sentence. Repeats and blanks are ignored."""
        english = (english or "").strip()
        if len(english) < 2:
            return
        with self._lock:
            if english in self._submitted:
                return
            self._submitted.add(english)
        try:
            self.queue.put_nowait((english, previous))
        except queue.Full:
            # Falling behind: the streaming translation still stands, and a
            # dropped refinement is better than an unbounded backlog.
            logger.warning("润色队列已满，跳过一句。")
            with self._lock:
                self._submitted.discard(english)

    def get(self, english: str) -> str | None:
        with self._lock:
            return self._results.get((english or "").strip())

    def refined_keys(self) -> list[str]:
        with self._lock:
            return list(self._results)

    @property
    def average_seconds(self) -> float:
        count = self.stats["count"]
        return self.stats["total_seconds"] / count if count else 0.0


class RefinementMerger:
    """Feed finished sentences to a refiner and fold results back into snapshots.

    A line is final once a later line exists: the ASR has moved on and will not
    revise it (verified against the live pipeline -- superseded lines never
    changed again). Until then the line keeps the server's streaming draft.
    """

    def __init__(self, refiner: TranslationRefiner | None) -> None:
        self.refiner = refiner

    def process(self, snapshot):
        """Return the snapshot with refined translations substituted in."""
        if self.refiner is None:
            return snapshot

        lines = snapshot.speech_lines
        previous_text: str | None = None
        for index, line in enumerate(lines):
            is_final = index < len(lines) - 1
            if is_final:
                self.refiner.submit(line.text, previous_text)
            previous_text = line.text

        for line in snapshot.lines:
            if line.is_silence or not line.text.strip():
                continue
            refined = self.refiner.get(line.text)
            if refined:
                line.translation = refined
                line.refined = True
        return snapshot

    def flush_last(self, snapshot) -> None:
        """At end of meeting the trailing line is final too."""
        if self.refiner is None:
            return
        lines = snapshot.speech_lines
        if not lines:
            return
        previous = lines[-2].text if len(lines) > 1 else None
        self.refiner.submit(lines[-1].text, previous)
