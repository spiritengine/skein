"""Phase 4 §D probe: bit-flip rejection matrix vs real sigstore-python.

Loads each conformance corpus v0.3 bundle and, for every (byte, bit) position
in its JSON blob, computes:

  - factory_outcome: what the synthetic factory's bit-flip rejection logic
    would conclude. Mirrors signing._canonical_drift_exception: parse the
    blob, re-serialize via Bundle.to_json(), compare to the original
    canonical. Different => factory rejects.

  - real_outcome: what real sigstore-python actually does. Calls
    Verifier.staging(offline=True).verify_artifact(...) on the parsed bundle.

The 2x2 matrix:
   case 1: factory_no_op AND real_no_op       (no-op position)
   case 2: factory_rejects AND real_rejects   (both reject; healthy)
   case 3: factory_rejects AND real_accepts   (factory over-strict - bug)
   case 4: factory_accepts AND real_rejects   (factory under-strict)

§D's pass criterion: case 3 is empty or has documented benign reasons.

Run as a script, not via pytest:
    python tests/test_signing/bit_flip_matrix_probe.py [bundle_v3|...|all]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import logging

# Resolve skein against the project root the probe lives in (parents[2]) before
# the editable-install finder, so a worktree copy of this probe exercises the
# worktree's skein rather than the main repo's installed package.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sigstore.models import Bundle
from sigstore.verify import Verifier
from sigstore.verify.policy import UnsafeNoOp

# Probe the real factory drift detector rather than re-implementing it here,
# so the verification gate stays in lockstep with production code as it tightens
# or loosens per-region.
from skein.signing import _canonical_drift_exception

# UnsafeNoOp emits a warning on every verify; silence it so progress output
# stays readable. We're intentionally bypassing identity policy because §D
# probes integrity rejection, not identity.
logging.getLogger("sigstore.verify.policy").setLevel(logging.ERROR)

CORPUS = Path(__file__).resolve().parents[1] / "conformance" / "corpus"


def load_corpus(stem: str, subdir: str = "", suffix: str = ".sigstore"):
    base = CORPUS / subdir if subdir else CORPUS
    artifact = (base / f"{stem}.txt").read_bytes()
    blob = (base / f"{stem}.txt{suffix}").read_bytes()
    return artifact, blob


def factory_outcome(blob_bytes: bytes, original_canonical: str):
    """Run the real signing._canonical_drift_exception against `blob_bytes`.

    Returns one of:
      ("accept", None)         - drift detector returned None
      ("reject_parse", reason) - Bundle.from_json raises
      ("reject_drift", region) - drift detector returned an exception; region
                                 names the first changed top-level key for
                                 reporting (the detector itself decides).
    """
    try:
        blob = blob_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ("reject_parse", f"utf8: {exc.reason}")
    try:
        bundle = Bundle.from_json(blob)
    except Exception as exc:
        return ("reject_parse", type(exc).__name__)
    meta = {"_canonical_json": original_canonical}
    try:
        drift = _canonical_drift_exception(meta, bundle)
    except Exception as exc:
        return ("reject_drift", f"detector_raised: {type(exc).__name__}")
    if drift is None:
        return ("accept", None)
    try:
        canonical = bundle.to_json()
    except Exception:
        return ("reject_drift", "to_json_failed")
    return ("reject_drift", _drift_region(original_canonical, canonical))


def _drift_region(orig: str, cur: str) -> str:
    """Identify the first differing top-level region in a drifted canonical."""
    try:
        o = json.loads(orig)
        c = json.loads(cur)
    except Exception:
        return "unparseable"
    # messageSignature
    if o.get("messageSignature") != c.get("messageSignature"):
        return "messageSignature"
    vm_o = o.get("verificationMaterial") or {}
    vm_c = c.get("verificationMaterial") or {}
    if vm_o.get("certificate") != vm_c.get("certificate"):
        return "verificationMaterial.certificate"
    te_o = (vm_o.get("tlogEntries") or [{}])[0] or {}
    te_c = (vm_c.get("tlogEntries") or [{}])[0] or {}
    for k in (
        "canonicalizedBody",
        "inclusionPromise",
        "inclusionProof",
        "logId",
        "integratedTime",
        "kindVersion",
        "logIndex",
    ):
        if te_o.get(k) != te_c.get(k):
            return f"tlogEntries[0].{k}"
    if vm_o.get("timestampVerificationData") != vm_c.get(
        "timestampVerificationData"
    ):
        return "timestampVerificationData"
    if o.get("mediaType") != c.get("mediaType"):
        return "mediaType"
    return "other"


def real_outcome(blob_bytes: bytes, artifact: bytes, verifier, policy):
    """Run real sigstore-python verify on the blob.

    Returns ("accept", None) or ("reject", reason_class_name).
    """
    try:
        blob = blob_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ("reject", "UnicodeDecodeError")
    try:
        bundle = Bundle.from_json(blob)
    except Exception as exc:
        return ("reject", f"from_json:{type(exc).__name__}")
    try:
        verifier.verify_artifact(artifact, bundle, policy)
    except Exception as exc:
        return ("reject", type(exc).__name__)
    return ("accept", None)


def run(stem: str, *, subdir: str = "", suffix: str = ".sigstore",
        sample: int | None = None, progress: int = 1000):
    artifact, blob = load_corpus(stem, subdir, suffix)
    print(f"--- bundle: {stem}  ({len(blob)} bytes, {len(blob)*8} bits) ---")

    # Sanity: original passes
    original_bundle = Bundle.from_json(blob.decode("utf-8"))
    original_canonical = original_bundle.to_json()

    verifier = Verifier.staging(offline=True)
    policy = UnsafeNoOp()

    f_orig = factory_outcome(blob, original_canonical)
    r_orig = real_outcome(blob, artifact, verifier, policy)
    print(f"  original: factory={f_orig}  real={r_orig}")
    if f_orig[0] != "accept" or r_orig[0] != "accept":
        print("  WARNING: original does not pass both paths.")

    total_positions = len(blob) * 8
    positions = range(total_positions)
    if sample is not None:
        import random
        positions = random.sample(range(total_positions), sample)

    matrix = {
        "case1_both_noop": 0,
        "case2_both_reject": 0,
        "case3_factory_overstrict": 0,
        "case4_factory_understrict": 0,
    }
    case3_positions = []  # full detail
    case4_positions = []  # full detail
    factory_regions = {}  # region -> count
    real_reasons = {}  # reason -> count

    t0 = time.time()
    work = bytearray(blob)
    for idx, pos in enumerate(positions):
        byte_idx, bit_idx = divmod(pos, 8)
        work[byte_idx] ^= 1 << bit_idx
        try:
            f_status, f_detail = factory_outcome(bytes(work), original_canonical)
            r_status, r_detail = real_outcome(bytes(work), artifact, verifier, policy)
        finally:
            work[byte_idx] ^= 1 << bit_idx  # restore

        f_rejects = f_status != "accept"
        r_rejects = r_status != "accept"

        if not f_rejects and not r_rejects:
            matrix["case1_both_noop"] += 1
        elif f_rejects and r_rejects:
            matrix["case2_both_reject"] += 1
            if f_status == "reject_drift":
                factory_regions[f_detail] = factory_regions.get(f_detail, 0) + 1
            real_reasons[r_detail] = real_reasons.get(r_detail, 0) + 1
        elif f_rejects and not r_rejects:
            matrix["case3_factory_overstrict"] += 1
            case3_positions.append(
                (byte_idx, bit_idx, chr(blob[byte_idx]) if 32 <= blob[byte_idx] < 127 else f"\\x{blob[byte_idx]:02x}", f_status, f_detail)
            )
        else:
            matrix["case4_factory_understrict"] += 1
            case4_positions.append(
                (byte_idx, bit_idx, chr(blob[byte_idx]) if 32 <= blob[byte_idx] < 127 else f"\\x{blob[byte_idx]:02x}", r_detail)
            )

        if (idx + 1) % progress == 0:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed
            eta = (len(positions) - (idx + 1)) / rate
            print(f"  [{idx+1}/{len(positions)}]  {rate:.1f} pos/s  eta {eta:.0f}s  "
                  f"matrix={matrix}", flush=True)

    elapsed = time.time() - t0
    total = sum(matrix.values())
    print()
    print(f"=== {stem} matrix ({elapsed:.1f}s, {total} positions) ===")
    for k, v in matrix.items():
        pct = 100.0 * v / max(1, total)
        print(f"  {k:34s}  {v:6d}  ({pct:5.2f}%)")

    if factory_regions:
        print("\n  factory rejection regions (case 2):")
        for region, count in sorted(factory_regions.items(), key=lambda x: -x[1]):
            print(f"    {region:40s}  {count}")

    if real_reasons:
        print("\n  real verify exception classes (case 2):")
        for reason, count in sorted(real_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:40s}  {count}")

    if case3_positions:
        print(f"\n  CASE 3 (factory over-strict, real accepts) — {len(case3_positions)} positions:")
        for byte_idx, bit_idx, ch, f_status, f_detail in case3_positions[:50]:
            print(f"    byte={byte_idx:5d} bit={bit_idx}  char={ch!r:8s}  "
                  f"factory={f_status:14s} detail={f_detail}")
        if len(case3_positions) > 50:
            print(f"    ... ({len(case3_positions) - 50} more)")

    if case4_positions:
        print(f"\n  CASE 4 (factory under-strict, real rejects) — {len(case4_positions)} positions:")
        for byte_idx, bit_idx, ch, r_detail in case4_positions[:50]:
            print(f"    byte={byte_idx:5d} bit={bit_idx}  char={ch!r:8s}  real={r_detail}")
        if len(case4_positions) > 50:
            print(f"    ... ({len(case4_positions) - 50} more)")

    return matrix, case3_positions, case4_positions


BUNDLES = [
    ("bundle_v3", "", ".sigstore"),
    ("bundle_v3_alt", "", ".sigstore"),
    ("bundle_v3_no_signed_time", "", ".sigstore.json"),
    ("bundle", "tsa", ".sigstore"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="bundle_v3",
                    help="bundle stem, 'all', or a stem in BUNDLES list")
    ap.add_argument("--sample", type=int, default=None,
                    help="sample N positions instead of exhaustive")
    ap.add_argument("--progress", type=int, default=1000,
                    help="log progress every N positions")
    args = ap.parse_args()

    targets = BUNDLES if args.target == "all" else [
        (stem, sub, suf) for stem, sub, suf in BUNDLES if stem == args.target
    ]
    if not targets:
        print(f"Unknown target: {args.target}")
        print(f"Choices: {[b[0] for b in BUNDLES]} or 'all'")
        sys.exit(2)

    for stem, sub, suf in targets:
        try:
            run(stem, subdir=sub, suffix=suf,
                sample=args.sample, progress=args.progress)
        except Exception as exc:
            print(f"  FAILED on {stem}: {type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
