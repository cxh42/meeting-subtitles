"""Back up the downloaded models, and put them back after a reinstall.

The four models this app uses come to about 23 GB. Downloading them again is
slow at the best of times, and on a network where ``huggingface.co`` does not
resolve it is the kind of slow that looks like a hang. Copying the cache onto
another disk before wiping the system, and copying it back afterwards, turns
that into a file copy.

    python tools/models.py backup  --to /mnt/backup/meeting-models
    python tools/models.py restore --from /mnt/backup/meeting-models
    python tools/models.py status
    python tools/models.py prune

Two details matter and are handled here:

*   The Hugging Face cache stores each file once under ``blobs/`` and links to
    it from ``snapshots/``. The backup dereferences those links and skips
    ``blobs/`` entirely, so the copy holds one plain copy of each file and can
    live on a filesystem with no symlink support. ``huggingface_hub`` is happy
    either way -- it resolves ``refs/<branch>`` to a revision and then reads
    ``snapshots/<revision>/<file>``, never caring whether that is a link.
*   A repo can hold revisions nothing points at any more. Only the revisions
    named in ``refs/`` are copied, and ``prune`` is what deletes them -- they
    are pure waste, and on this machine they were 7.1 GB of it.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

DEFAULT_WHISPER = "large-v3"
DEFAULT_NLLB = "1.3B"
DEFAULT_REFINE = "Qwen/Qwen3-4B-Instruct-2507"


def hf_cache() -> Path:
    explicit = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def whisper_cache() -> Path:
    default = Path.home() / ".cache"
    return Path(os.environ.get("XDG_CACHE_HOME", default)) / "whisper"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


def repo_names(args) -> list[str]:
    names = [f"models--Systran--faster-whisper-{args.whisper}",
             f"models--facebook--nllb-200-distilled-{args.nllb}"]
    if args.refine:
        names.append("models--" + args.refine.replace("/", "--"))
    return names


def wanted_refs(repo: Path, all_revisions: bool) -> list[Path]:
    """The ref files worth copying.

    ``main`` alone by default. A cache also accumulates refs for pull requests
    (``refs/pr/5``), and those can point at a whole extra copy of the weights
    -- 5.1 GB of NLLB safetensors on this machine -- that nothing will ever
    load, because the app asks for the repo without naming a revision.
    """
    refs = repo / "refs"
    if not refs.is_dir():
        return []
    found = sorted(ref for ref in refs.rglob("*") if ref.is_file())
    if all_revisions:
        return found
    main = [ref for ref in found if ref.relative_to(refs).as_posix() == "main"]
    return main or found


def referenced_revisions(repo: Path, all_revisions: bool = False) -> list[str]:
    """Revision hashes named by a ref; snapshots nothing points at are stale."""
    refs = wanted_refs(repo, all_revisions)
    if not refs:
        snapshots = repo / "snapshots"
        return [d.name for d in snapshots.iterdir()] if snapshots.is_dir() else []
    revisions = []
    for ref in refs:
        revision = ref.read_text(encoding="utf-8").strip()
        if revision and revision not in revisions:
            revisions.append(revision)
    return revisions


def plan_repo(repo: Path, destination: Path,
              all_revisions: bool = False) -> list[tuple[Path, Path]]:
    """(source, destination) for every file this repo needs, links resolved."""
    jobs: list[tuple[Path, Path]] = []
    for ref in wanted_refs(repo, all_revisions):
        jobs.append((ref, destination / ref.relative_to(repo)))
    for revision in referenced_revisions(repo, all_revisions):
        snapshot = repo / "snapshots" / revision
        if not snapshot.is_dir():
            continue
        for item in sorted(snapshot.rglob("*")):
            if item.is_symlink() or item.is_file():
                source = item.resolve()
                if source.is_file():
                    jobs.append((source, destination / item.relative_to(repo)))
    return jobs


def copy_jobs(jobs, dry_run: bool) -> tuple[int, int]:
    """Copy each pair, skipping files already present at the right size."""
    copied = skipped = 0
    for index, (source, target) in enumerate(jobs, 1):
        size = source.stat().st_size
        if target.exists() and target.stat().st_size == size:
            skipped += size
            continue
        copied += size
        print(f"  [{index}/{len(jobs)}] {target.name:<48} {human(size):>10}",
              flush=True)
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy to a sibling first: an interrupted run must not leave a
        # half-written file that looks complete to the next one.
        temporary = target.with_name(target.name + ".partial")
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    return copied, skipped


def build_plan(args, source_hub: Path, source_whisper: Path,
               target_hub: Path, target_whisper: Path):
    """Group the copy into named sets, and note what is missing."""
    plan, missing = [], []
    for name in repo_names(args):
        repo = source_hub / name
        if repo.is_dir():
            plan.append((name, plan_repo(repo, target_hub / name,
                                         args.all_revisions)))
        else:
            missing.append(name)
    checkpoint = source_whisper / f"{args.whisper}.pt"
    if checkpoint.is_file():
        plan.append(("whisper " + args.whisper,
                     [(checkpoint, target_whisper / checkpoint.name)]))
    else:
        missing.append(str(checkpoint))
    return plan, missing


def run_copy(args, source_hub, source_whisper, target_hub, target_whisper,
             verb: str) -> int:
    plan, missing = build_plan(args, source_hub, source_whisper,
                               target_hub, target_whisper)
    if missing:
        print("下面这些不在源目录里，将被跳过：", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print(file=sys.stderr)
    if not plan:
        print("没有找到任何模型。", file=sys.stderr)
        return 1

    total_copied = total_skipped = 0
    for name, jobs in plan:
        print(f"\n{name}  ({len(jobs)} 个文件)")
        copied, skipped = copy_jobs(jobs, args.dry_run)
        if skipped:
            print(f"  已存在，跳过 {human(skipped)}")
        total_copied += copied
        total_skipped += skipped

    head = "将要复制" if args.dry_run else f"已{verb}"
    print(f"\n{head} {human(total_copied)}"
          + (f"，跳过已存在的 {human(total_skipped)}" if total_skipped else ""))
    return 0


def cmd_backup(args) -> int:
    root = Path(args.to).expanduser()
    if not args.dry_run:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"错误: 无法创建 {root}: {exc}", file=sys.stderr)
            return 2
    print(f"源  : {hf_cache()}\n目标: {root}")
    code = run_copy(args, hf_cache(), whisper_cache(),
                    root / "huggingface" / "hub", root / "whisper", "备份")
    if code == 0 and not args.dry_run:
        print(f"""
重装系统后，用下面这条把模型放回去（不用重新下载）：

    python tools/models.py restore --from {root}
""")
    return code


def cmd_restore(args) -> int:
    root = Path(getattr(args, "from")).expanduser()
    if not root.is_dir():
        print(f"错误: 找不到备份目录 {root}", file=sys.stderr)
        return 2
    print(f"源  : {root}\n目标: {hf_cache()}")
    return run_copy(args, root / "huggingface" / "hub", root / "whisper",
                    hf_cache(), whisper_cache(), "恢复")


def prune_repo(repo: Path) -> tuple[list[Path], int]:
    """Files in one repo that nothing will ever load, and their total size.

    Two kinds accumulate, both invisible until the disk fills:

    *   Revisions no ref points at any more. A ``refs/pr/*`` left over from
        resolving a repo once holds a whole second copy of the weights -- 5.5 GB
        of NLLB safetensors on this machine -- that nothing loads, because the
        app asks for the repo without naming a revision.
    *   ``*.incomplete`` blobs from a download that was interrupted. The next
        attempt starts a new file rather than resuming this one.
    """
    doomed: list[Path] = []
    keep = set(referenced_revisions(repo, all_revisions=False))

    snapshots = repo / "snapshots"
    if snapshots.is_dir():
        for revision in sorted(snapshots.iterdir()):
            if revision.is_dir() and revision.name not in keep:
                doomed.append(revision)

    refs = repo / "refs"
    if refs.is_dir():
        for ref in sorted(refs.rglob("*")):
            if ref.is_file() and ref.read_text(encoding="utf-8").strip() not in keep:
                doomed.append(ref)

    # A blob survives only while a revision we are keeping still links to it.
    live = set()
    for revision in keep:
        snapshot = snapshots / revision
        if not snapshot.is_dir():
            continue
        for item in snapshot.rglob("*"):
            if item.is_symlink() or item.is_file():
                live.add(item.resolve())
    blobs = repo / "blobs"
    if blobs.is_dir():
        for blob in sorted(blobs.iterdir()):
            if blob.is_file() and blob.resolve() not in live:
                doomed.append(blob)

    total = 0
    for item in doomed:
        if item.is_dir():
            total += sum(f.stat().st_size for f in item.rglob("*")
                         if f.is_file() and not f.is_symlink())
        else:
            total += item.stat().st_size
    return doomed, total


def cmd_prune(args) -> int:
    """Report reclaimable space, and delete it only when told to.

    Listing is the default on purpose: this walks a cache the user may share
    with other projects, and a wrong guess here costs a re-download, not a
    re-run.
    """
    print(f"缓存: {hf_cache()}\n")
    grand = 0
    plans = []
    for name in repo_names(args):
        repo = hf_cache() / name
        if not repo.is_dir():
            continue
        doomed, total = prune_repo(repo)
        if not doomed:
            continue
        plans.append((name, doomed))
        grand += total
        print(f"{name}  可回收 {human(total)}")
        for item in doomed:
            kind = "旧版本" if item.parent.name == "snapshots" else (
                "未完成" if ".incomplete" in item.name else "无引用")
            print(f"  [{kind}] {item.relative_to(repo)}")
        print()

    if not plans:
        print("没有可回收的文件。")
        return 0
    if not args.yes:
        print(f"合计可回收 {human(grand)}。确认无误后加 --yes 执行：")
        print("    python tools/models.py prune --yes")
        return 0

    for _name, doomed in plans:
        for item in doomed:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
    print(f"已回收 {human(grand)}。")
    return 0


def cmd_status(args) -> int:
    """What is cached locally, and how big -- run this before wiping a disk."""
    print(f"Hugging Face 缓存: {hf_cache()}")
    print(f"Whisper 缓存     : {whisper_cache()}\n")
    total = 0
    for name in repo_names(args):
        repo = hf_cache() / name
        if repo.is_dir():
            jobs = plan_repo(repo, Path("/dev/null"), args.all_revisions)
            size = sum(source.stat().st_size for source, _ in jobs)
            total += size
            print(f"  ✓ {name:<46} {human(size):>10}")
        else:
            print(f"  ✗ {name:<46} {'未下载':>10}")
    checkpoint = whisper_cache() / f"{args.whisper}.pt"
    if checkpoint.is_file():
        size = checkpoint.stat().st_size
        total += size
        print(f"  ✓ {'whisper ' + args.whisper:<46} {human(size):>10}")
    else:
        print(f"  ✗ {'whisper ' + args.whisper:<46} {'未下载':>10}")
    print(f"\n  {'合计':<46} {human(total):>10}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="备份 / 恢复会议字幕用到的本地模型（约 23 GB）。")
    parser.add_argument("--whisper", default=DEFAULT_WHISPER)
    parser.add_argument("--nllb", default=DEFAULT_NLLB)
    parser.add_argument("--refine", default=DEFAULT_REFINE,
                        help="整句润色模型；传空字符串可跳过（省 7.6 GB）")
    parser.add_argument("--all-revisions", action="store_true",
                        help="连同 pull-request 等其它 revision 一起处理（通常没必要）")
    parser.add_argument("--dry-run", action="store_true", help="只列出，不复制")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="把模型复制到备份目录")
    backup.add_argument("--to", required=True, metavar="目录")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="从备份目录复制回缓存")
    restore.add_argument("--from", required=True, metavar="目录", dest="from")
    restore.set_defaults(func=cmd_restore)

    sub.add_parser("status", help="列出本地已有哪些模型及大小"
                   ).set_defaults(func=cmd_status)

    prune = sub.add_parser("prune", help="列出并（加 --yes 时）删除缓存里没人用的旧版本")
    prune.add_argument("--yes", action="store_true", help="真的删除，不加只列出")
    prune.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
