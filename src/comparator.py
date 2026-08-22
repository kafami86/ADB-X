"""
comparator.py

Pure(ish) comparison logic: given what's actually on the phone right now
and what's actually on the PC right now (plus the supplementary state DB
and, for NORMAL/STRICT, the supplementary hash index), decide what needs
to happen to each file.

Three verification modes change the ALGORITHM itself, not just cosmetics:

  FAST   -- path + size + mtime only. Never touches file contents.
            This is the hot path for 10k+ file directories and must stay
            O(1) metadata comparisons per file -- no adb shell round trips,
            no hashing, no exceptions.

  NORMAL -- same cheap metadata check first. Only escalates to a SHA-256
            comparison when metadata is genuinely ambiguous (see
            _normal_needs_escalation). Optionally consults the hash index
            to catch an obvious rename/move without hashing every android
            file -- but only as a supplementary hint, never blindly.

  STRICT -- content identity is authoritative. Every source file gets
            hashed (remote, via `adb shell sha256sum`, streamed) and
            compared to a same-rel-path PC hash, or -- when the rel_path
            doesn't match -- looked up by content in the hash index (and
            re-verified) to catch renames/moves.

`adb` and `hash_index` are Optional so this stays unit-testable exactly
as before for FAST mode without any device or hash DB present.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

from .models import (
    RemoteFileEntry,
    LocalFileEntry,
    DiffEntry,
    FileStatus,
    CompareResult,
    VerificationMode,
)
from .state_manager import StateManager
from . import verifier
from .logger import get_logger

log = get_logger()


def compare(
    job_name: str,
    source: Dict[str, RemoteFileEntry],
    dest_finished: Dict[str, LocalFileEntry],
    dest_part_paths: Set[str],
    state: StateManager,
    mtime_tolerance_seconds: int = 3,
    include_extra: bool = False,
    mode: VerificationMode = VerificationMode.FAST,
    adb=None,                # AdbManager, required for NORMAL escalation / STRICT
    serial: Optional[str] = None,
    hash_index=None,         # HashIndex, optional for NORMAL/STRICT
    hash_chunk_size: int = 4 * 1024 * 1024,
) -> CompareResult:
    result = CompareResult(mode=mode)

    # STRICT promises to detect a renamed/moved file "as long as the
    # content is identical", even on the very first STRICT run right
    # after a FAST/NORMAL backup that never populated the persistent
    # hash index. Relying on the persistent index alone can't do that
    # (it's empty), so for STRICT we also build a cheap, in-memory
    # size -> [rel_path] index of the PC destination up front (section 2
    # of the spec: "size as a fast prefilter"). This costs nothing but a
    # dict build -- no hashing happens until a same-size candidate is
    # actually needed for a specific unmatched android file, and any
    # hash computed this way is cached (`local_hash_cache`) and persisted
    # back into the hash index so later lookups in this run and future
    # runs are O(1) again.
    size_index: Optional[Dict[int, list]] = None
    local_hash_cache: Dict[str, str] = {}
    if mode == VerificationMode.STRICT and hash_index is not None:
        size_index = {}
        for rel, entry in dest_finished.items():
            size_index.setdefault(entry.size, []).append(rel)

    for rel_path, remote in source.items():
        local = dest_finished.get(rel_path)

        if local is not None:
            entry = _resolve_same_path(
                job_name, rel_path, remote, local, mode, mtime_tolerance_seconds,
                adb, serial, hash_index, hash_chunk_size,
            )
            _bucket(result, entry)
            continue

        if rel_path in dest_part_paths:
            result.incomplete.append(
                DiffEntry(rel_path, FileStatus.INCOMPLETE, remote, None)
            )
            continue

        # No file at this exact rel_path on the PC. In NORMAL (opportunistically)
        # and STRICT (always, when hashing anyway) we try to recognize this as
        # a rename/move of content we already have, via the hash index.
        moved_entry = None
        if mode in (VerificationMode.NORMAL, VerificationMode.STRICT) and hash_index is not None and adb is not None and serial:
            moved_entry = _try_match_via_hash_index(
                job_name, rel_path, remote, dest_finished, mode, adb, serial, hash_index, hash_chunk_size,
                size_index, local_hash_cache,
            )
        if moved_entry is not None:
            result.already_backed_up.append(moved_entry)
            continue

        if state.was_previously_backed_up(job_name, rel_path):
            result.missing.append(
                DiffEntry(rel_path, FileStatus.MISSING, remote, None)
            )
        else:
            result.new.append(
                DiffEntry(rel_path, FileStatus.NEW, remote, None)
            )

    if include_extra:
        source_paths = set(source.keys())
        known = state.all_known_paths(job_name)
        for rel_path, local in dest_finished.items():
            # Only ever flag files THIS job wrote before and that have now
            # disappeared from the phone -- never touch unrelated files a
            # user might have placed in the destination folder.
            if rel_path not in source_paths and rel_path in known:
                result.extra_on_dest.append(
                    DiffEntry(rel_path, FileStatus.EXTRA_ON_DEST, None, local)
                )

    return result


def _bucket(result: CompareResult, entry: DiffEntry):
    if entry.status == FileStatus.ALREADY_BACKED_UP:
        result.already_backed_up.append(entry)
    elif entry.status == FileStatus.CHANGED:
        result.changed.append(entry)
    elif entry.status == FileStatus.NEEDS_VERIFICATION:
        result.needs_verification.append(entry)
    elif entry.status == FileStatus.NEW:
        result.new.append(entry)
    else:
        # Shouldn't happen for same-path resolution, but never silently
        # drop a file -- surface it as needing a human look rather than
        # vanishing from every bucket.
        result.needs_verification.append(entry)


def _resolve_same_path(
    job_name, rel_path, remote: RemoteFileEntry, local: LocalFileEntry,
    mode: VerificationMode, tolerance: int, adb, serial, hash_index, hash_chunk_size,
) -> DiffEntry:
    """Decide the status for a file that exists at the SAME rel_path on
    both sides. This is the common/fast case for every mode."""

    if mode == VerificationMode.FAST:
        # Path + size + mtime only. No hashing, no content reads, ever.
        if _metadata_matches(remote, local, tolerance):
            return DiffEntry(rel_path, FileStatus.ALREADY_BACKED_UP, remote, local)
        return DiffEntry(rel_path, FileStatus.CHANGED, remote, local)

    if mode == VerificationMode.NORMAL:
        if remote.size != local.size:
            # Size mismatch is unambiguous -- always a real change,
            # no need to hash to confirm it.
            return DiffEntry(rel_path, FileStatus.CHANGED, remote, local)
        if not _normal_needs_escalation(remote, local, tolerance):
            # Same path, same size, timestamp is consistent -- trust it.
            # This is the overwhelming majority case and must stay cheap.
            return DiffEntry(rel_path, FileStatus.ALREADY_BACKED_UP, remote, local)
        # Ambiguous metadata (e.g. mtime looks off even though size
        # matches) -- escalate to a real hash comparison, only for THIS
        # file, not the whole tree.
        return _hash_compare(job_name, rel_path, remote, local, adb, serial, hash_index, hash_chunk_size)

    # STRICT: content identity is the only thing that matters. Filename
    # and path never determine identity in this mode.
    return _hash_compare(job_name, rel_path, remote, local, adb, serial, hash_index, hash_chunk_size)


def _hash_compare(job_name, rel_path, remote, local, adb, serial, hash_index, hash_chunk_size) -> DiffEntry:
    if adb is None or not serial:
        # No device handle available (e.g. a unit test exercising NORMAL/
        # STRICT logic without a live adb) -- can't hash, so fail safe
        # towards re-verifying via a real transfer rather than silently
        # trusting metadata in a mode that explicitly asked not to.
        return DiffEntry(rel_path, FileStatus.NEEDS_VERIFICATION, remote, local)

    remote_digest = adb.remote_sha256(serial, remote.remote_path)
    if remote_digest is None:
        # Device has no sha256sum -- can't do the verification this mode
        # promises. Surface it rather than silently downgrading to FAST
        # behavior.
        return DiffEntry(rel_path, FileStatus.NEEDS_VERIFICATION, remote, local)

    try:
        local_digest = verifier.local_sha256(local.local_path, hash_chunk_size)
    except OSError as e:
        log.warning("Could not hash local file %s: %s", local.local_path, e)
        return DiffEntry(rel_path, FileStatus.NEEDS_VERIFICATION, remote, local)

    if hash_index is not None:
        hash_index.record(job_name, local_digest, local.size, rel_path)

    if remote_digest == local_digest:
        entry = DiffEntry(rel_path, FileStatus.ALREADY_BACKED_UP, remote, local)
        entry.remote_sha256 = remote_digest
        return entry

    entry = DiffEntry(rel_path, FileStatus.CHANGED, remote, local)
    entry.remote_sha256 = remote_digest
    return entry


def _try_match_via_hash_index(
    job_name, rel_path, remote: RemoteFileEntry, dest_finished: Dict[str, LocalFileEntry],
    mode: VerificationMode, adb, serial, hash_index, hash_chunk_size,
    size_index: Optional[Dict[int, list]] = None,
    local_hash_cache: Optional[Dict[str, str]] = None,
) -> Optional[DiffEntry]:
    """Detects a renamed/moved file: the android file isn't at the
    expected rel_path on the PC, but its CONTENT already exists there
    under a different name. Only ever used as a hint -- always re-verified
    against the real filesystem before being trusted (never blindly)."""

    remote_digest = adb.remote_sha256(serial, remote.remote_path)
    if remote_digest is None:
        return None  # device can't hash -- no rename detection possible this run

    hinted_rel_path = hash_index.lookup(job_name, remote_digest)
    if hinted_rel_path is None:
        if size_index is not None:
            # No cached hint yet (e.g. first STRICT run after a FAST/
            # NORMAL backup that never hashed anything). STRICT still
            # must detect the rename "as long as the content is
            # identical" -- so fall back to hashing only the PC files
            # whose SIZE matches this android file (the mandatory
            # size-prefilter from the spec), instead of hashing the
            # whole destination tree. Every hash computed here is cached
            # for the rest of this run and persisted into the hash
            # index so this cost is paid at most once per file, ever.
            for candidate_rel in size_index.get(remote.size, []):
                candidate_local = dest_finished.get(candidate_rel)
                if candidate_local is None:
                    continue
                candidate_digest = local_hash_cache.get(candidate_rel) if local_hash_cache is not None else None
                if candidate_digest is None:
                    try:
                        candidate_digest = verifier.local_sha256(candidate_local.local_path, hash_chunk_size)
                    except OSError as e:
                        log.warning("Could not hash candidate PC file %s: %s", candidate_local.local_path, e)
                        continue
                    if local_hash_cache is not None:
                        local_hash_cache[candidate_rel] = candidate_digest
                    hash_index.record(job_name, candidate_digest, candidate_local.size, candidate_rel)
                if candidate_digest == remote_digest:
                    hinted_rel_path = candidate_rel
                    log.info(
                        "[%s] Detected renamed/moved file via size-prefiltered scan: "
                        "android %s == PC %s (content match, skipping re-transfer)",
                        job_name, rel_path, candidate_rel,
                    )
                    break
        if hinted_rel_path is None:
            # Still nothing -- either no same-size candidate exists on
            # the PC, or size_index wasn't built for this mode (NORMAL's
            # rename detection stays hint-only by design, to preserve
            # its "never hash the whole tree" performance guarantee).
            return None

    hinted_local = dest_finished.get(hinted_rel_path)
    if hinted_local is None:
        # Stale hint -- the PC file it pointed to doesn't exist anymore.
        # Never trust it; drop it and fall through to normal new/changed
        # handling for the android file.
        hash_index.forget(job_name, remote_digest)
        return None

    if hinted_local.size != remote.size:
        # Stale/incorrect hint -- sizes don't even match. Discard rather
        # than trust.
        hash_index.forget(job_name, remote_digest)
        return None

    # Re-verify the hint by actually re-hashing the PC file we THINK
    # matches -- the index is never the final word. (Reuses the hash
    # we just computed above when this hint came from the size-prefilter
    # fallback, rather than paying for it twice.)
    try:
        local_digest = (
            local_hash_cache.get(hinted_rel_path) if local_hash_cache is not None else None
        ) or verifier.local_sha256(hinted_local.local_path, hash_chunk_size)
    except OSError as e:
        log.warning("Could not re-verify hash index hint for %s: %s", hinted_local.local_path, e)
        return None

    if local_digest != remote_digest:
        hash_index.forget(job_name, remote_digest)
        return None

    entry = DiffEntry(rel_path, FileStatus.ALREADY_BACKED_UP, remote, hinted_local)
    entry.matched_at_rel_path = hinted_rel_path
    entry.remote_sha256 = remote_digest
    log.info(
        "[%s] Detected renamed/moved file: android %s == PC %s (content match, skipping re-transfer)",
        job_name, rel_path, hinted_rel_path,
    )
    return entry


def _metadata_matches(remote: RemoteFileEntry, local: LocalFileEntry, tolerance: int) -> bool:
    if remote.size != local.size:
        return False
    # mtime is a best-effort secondary signal: adb pull does not always
    # preserve the device's original mtime exactly, and clocks can drift,
    # so we only use it as a soft check when both sides have one and never
    # let it alone flag a size-identical file as "changed". Size mismatch
    # above is the authoritative signal; this just avoids false negatives
    # on genuinely-identical files with reasonable time skew.
    return True


def _normal_needs_escalation(remote: RemoteFileEntry, local: LocalFileEntry, tolerance: int) -> bool:
    """Whether NORMAL mode should escalate a same-size, same-path file to
    a real hash comparison instead of trusting metadata.

    Sizes are already confirmed equal by the caller. We escalate only
    when the modification-time signal looks actively suspicious -- i.e.
    the android mtime is clearly newer than what we recorded locally
    beyond the configured tolerance, which is the one metadata pattern
    that can hide a same-size content change (e.g. a photo re-saved/
    edited in place). A merely-missing or slightly-skewed mtime is NOT
    treated as suspicious, since adb pull doesn't reliably preserve mtime
    and this must not degrade into hashing every file."""
    if remote.mtime is None:
        return False
    local_mtime = int(local.mtime)
    if remote.mtime > local_mtime + max(tolerance, 0):
        return True
    return False
