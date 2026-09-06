"""Domain presets: terminology hints for the ASR and the refiner.

Two very different consumers:

- The ASR takes a flat phrase list. Whisper conditions on it, which is the
  single biggest lever on accented speech -- a term in the list is recognised,
  the same term absent is guessed at phonetically.
- The refiner takes prose instructions. An LLM needs to know the *register*,
  not just the words: in a CS/AI meeting "attention" is 注意力, not 注意, and
  "training a model" is 训练, never 培训.
"""

from dataclasses import dataclass


@dataclass
class Domain:
    key: str
    label: str
    #: Phrase list passed to the ASR via the ?context= query parameter.
    terms: list[str]
    #: Extra guidance appended to the refiner's system prompt.
    guidance: str

    @property
    def context(self) -> str:
        return ", ".join(self.terms)


CS_AI = Domain(
    key="cs-ai",
    label="计算机 / 人工智能研究",
    # Only what the ASR is likely to get wrong: acronyms, coined names and
    # field-specific jargon. Ordinary English words ("attention", "baseline")
    # are recognised fine already, and the server caps the phrase list at
    # MAX_CONTEXT_CHARS -- padding it with easy words dilutes the conditioning
    # on the hard ones.
    terms=[
        "LoRA", "RLHF", "DPO", "MoE", "RAG", "LLM", "VLM", "SOTA", "OOM",
        "FLOPs", "bfloat16", "fp8", "KV cache", "FlashAttention", "vLLM",
        "PyTorch", "JAX", "CUDA", "Triton", "DeepSpeed", "Megatron",
        "tokenizer", "embedding", "transformer", "softmax", "logits",
        "perplexity", "WER", "BLEU", "F1", "AUC",
        "ablation", "hyperparameter", "backprop", "overfitting",
        "quantization", "distillation", "fine-tuning", "pre-training",
        "zero-shot", "few-shot", "in-context learning", "chain of thought",
        "scaling law", "context window", "tensor parallelism",
        "hallucination", "alignment", "guardrails", "prompt injection",
        "arXiv", "preprint", "rebuttal", "camera ready", "supplementary",
        "NeurIPS", "ICML", "ICLR", "ACL", "EMNLP", "CVPR", "ECCV",
    ],
    guidance=(
        "This is a computer-science / AI research meeting. Use the terminology "
        "Chinese researchers in this field actually use:\n"
        "- attention 注意力, embedding 嵌入, fine-tuning 微调, pre-training 预训练, "
        "distillation 蒸馏, quantization 量化, inference 推理, ablation study 消融实验, "
        "baseline 基线, benchmark 基准, overfitting 过拟合, gradient 梯度, "
        "hallucination 幻觉, alignment 对齐, retrieval 检索, prompt 提示词, "
        "scaling law 缩放定律, throughput 吞吐, latency 延迟.\n"
        "- 'train a model' is 训练模型, never 培训.\n"
        "- Keep model names, library names, metric names, conference names and "
        "acronyms in English (GPT-4, PyTorch, F1, NeurIPS, LoRA, RAG, KV cache).\n"
        "- Keep the register of a research discussion: concise and technical, "
        "not marketing prose."
    ),
)

GENERAL = Domain(
    key="general",
    label="通用会议",
    terms=[],
    guidance=(
        "This is a general business meeting. Keep product names, acronyms and "
        "technical terms in English where that is how they are normally said in "
        "Chinese workplaces."
    ),
)

DOMAINS: dict[str, Domain] = {d.key: d for d in (CS_AI, GENERAL)}
DEFAULT_DOMAIN = CS_AI.key


def get(key: str) -> Domain:
    return DOMAINS.get(key or DEFAULT_DOMAIN, CS_AI)


#: The server rejects a longer ?context= (session_asr_proxy.MAX_SESSION_CONTEXT_CHARS).
MAX_CONTEXT_CHARS = 1000


#: Wrapper that makes the term list read as prose. Whisper conditions on this
#: as if it were the preceding transcript and continues in the same style, so
#: a bare comma-separated list teaches it to emit comma-separated fragments --
#: and, measured on real audio, to hallucinate more of the list before the
#: speech starts ("YouTube, YouTube.com/NorthstarIT And."). Wrapping the terms
#: in a finished sentence removes both effects.
_PROMPT_TEMPLATE = "The following is a meeting transcript. Terms that appear: {}."


def build_context(domain_key: str, extra: str = "") -> str:
    """Decoder prompt for the ASR: the domain preset plus the user's own terms.

    The user's terms go first -- the names of the actual participants matter
    far more than a generic vocabulary, so they must survive the length cap.
    Truncation happens at a term boundary, never mid-word, and the result is
    always a complete sentence.
    """
    parts = []
    if extra and extra.strip():
        parts.append(extra.strip().rstrip(",").rstrip("."))
    preset = get(domain_key).context
    if preset:
        parts.append(preset)
    terms = ", ".join(parts)
    if not terms:
        return ""

    budget = MAX_CONTEXT_CHARS - len(_PROMPT_TEMPLATE.format(""))
    if len(terms) > budget:
        clipped = terms[:budget]
        cut = clipped.rfind(",")
        terms = clipped[:cut] if cut > 0 else clipped
    return _PROMPT_TEMPLATE.format(terms.rstrip(", "))
