#!/usr/bin/env python3
"""In-project CLIP similarity adapter for hand.tools.gate_clipsim.

Protocol (fixed by gate_clipsim):  --image PATH --reference PATH
prints exactly one JSON object to stdout: {"score": <cosine in [-1, 1]>}.

The heavy deps (torch + OpenAI clip, ViT-L/14, weights expected in the
local clip cache) may not exist in the project venv, so if they cannot
be imported in the current interpreter this script re-executes itself
under a CLIP-capable python: $HAND_CLIPSIM_PYTHON, else the known
aesthetic-gate venv on this machine. No downloads, no network: if the
weights are not cached locally it fails loudly instead of fetching.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

REEXEC_FLAG = "CLIPSIM_ADAPTER_REEXEC"


def compute_score(image: str, reference: str) -> float:
    import clip
    import numpy as np
    import torch
    from PIL import Image

    device = "cpu"
    # Offline contract: OpenAI CLIP's load() downloads weights over urllib
    # when the cache is cold, ignoring HF offline switches (audit A12).
    # Refuse unless the exact weights file is already cached locally.
    weights = Path.home() / ".cache" / "clip" / "ViT-L-14.pt"
    if not weights.is_file():
        raise RuntimeError(f"CLIP weights not cached at {weights}; offline adapter will not download")
    model, preprocess = clip.load("ViT-L/14", device=device)

    def emb(path: str):
        x = preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(device)
        with torch.no_grad():
            e = model.encode_image(x).cpu().numpy()
        norm = np.linalg.norm(e, axis=-1, keepdims=True)
        # A zero/non-finite norm yields NaN embeddings; never let the clamp
        # below rewrite a degenerate score into a valid one (audit A02).
        if not np.all(np.isfinite(norm)) or np.any(norm == 0):
            raise ValueError("Degenerate CLIP embedding (zero or non-finite norm)")
        return (e / norm)[0]

    score = float(emb(image) @ emb(reference))
    if not np.isfinite(score):
        raise ValueError("Non-finite CLIP score")
    return max(-1.0, min(1.0, score))  # guard float overshoot (identical imgs can give 1.0000002)


def clip_python_candidates() -> list[str]:
    out = []
    env = os.environ.get("HAND_CLIPSIM_PYTHON")
    if env:
        out.append(env)
    out.append(os.path.expanduser("~/workspace/tools/aesthetic-gate/venv/bin/python"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--reference", required=True)
    args = ap.parse_args()

    try:
        import clip  # noqa: F401
        import torch  # noqa: F401
        have_deps = True
    except Exception:
        have_deps = False

    if have_deps:
        score = compute_score(args.image, args.reference)
        print(json.dumps({"score": score}))
        return 0

    if os.environ.get(REEXEC_FLAG):
        print("CLIP deps unavailable even after re-exec", file=sys.stderr)
        return 3

    for cand in clip_python_candidates():
        if os.path.isfile(cand) and os.access(cand, os.X_OK) and os.path.abspath(cand) != os.path.abspath(sys.executable):
            env = {**os.environ, REEXEC_FLAG: "1"}
            proc = subprocess.run(
                [cand, os.path.abspath(__file__), "--image", args.image, "--reference", args.reference],
                env=env, capture_output=True, text=True,
            )
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode

    print("No CLIP-capable python found (set HAND_CLIPSIM_PYTHON)", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
