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
import subprocess
import sys

REEXEC_FLAG = "CLIPSIM_ADAPTER_REEXEC"


def compute_score(image: str, reference: str) -> float:
    import clip
    import numpy as np
    import torch
    from PIL import Image

    device = "cpu"
    model, preprocess = clip.load("ViT-L/14", device=device)

    def emb(path: str):
        x = preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(device)
        with torch.no_grad():
            e = model.encode_image(x).cpu().numpy()
        return (e / np.linalg.norm(e, axis=-1, keepdims=True))[0]

    score = float(emb(image) @ emb(reference))
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
