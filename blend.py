#!/usr/bin/env python3
"""Fabrique une voix Supertonic custom par combinaison ponderee de presets.

Un fichier de voix Supertonic = 2 embeddings INDEPENDANTS :
  - style_ttl [1,50,256] = le TIMBRE (couleur de la voix)
  - style_dp  [1,8,16]   = le RYTHME / la duree des mots (prosodie)
Le predicteur de duree n'utilise QUE style_dp (cf. core.py) : c'est lui qui fait
qu'un mot "traine" ou est vif. On peut donc marier un timbre grave a un rythme vif.

Melanger des embeddings valides reste dans la distribution du modele. Aucune
dependance externe, 100% local.

Usage :
  # meme dosage pour timbre et rythme :
  python blend.py M1:0.5 M5:0.5 -o voices/C.json

  # timbre grave (Alex+Daniel) MAIS rythme surtout Alex (plus vif) :
  python blend.py M1:0.5 M5:0.5 --dp M1:1.0 -o voices/C-dpAlex.json
  python blend.py M1:0.5 M5:0.5 --dp M1:0.7 M5:0.3 -o voices/C-dpMix.json

Les presets par defaut sont lus dans le cache du modele par defaut du SDK
(supertonic-3). Lance d'abord `supertonic download` (ou un premier run) une fois.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from supertonic.config import DEFAULT_MODEL, get_model_cache_dir
    DEFAULT_STYLES_DIR = get_model_cache_dir(DEFAULT_MODEL) / "voice_styles"
except Exception:  # SDK absent : repli sur les presets locaux
    DEFAULT_STYLES_DIR = Path(__file__).parent / "assets" / "voice_styles"


def scale_add(acc, data, w):
    """acc += w * data, recursivement sur les listes imbriquees (sans numpy)."""
    if isinstance(data, list):
        if acc is None:
            acc = [None] * len(data)
        return [scale_add(acc[i], data[i], w) for i in range(len(data))]
    return (acc or 0.0) + w * data


def parse_specs(specs):
    names, weights = [], []
    for s in specs:
        name, _, w = s.partition(":")
        names.append(name)
        weights.append(float(w) if w else 1.0)
    total = sum(weights)
    return names, [w / total for w in weights]


def blend_field(styles_dir, key, names, weights):
    acc = None
    dims = typ = None
    for name, w in zip(names, weights):
        path = styles_dir / f"{name}.json"
        if not path.exists():
            raise SystemExit(f"Preset introuvable : {path}")
        v = json.loads(path.read_text(encoding="utf-8"))
        acc = scale_add(acc, v[key]["data"], w)
        dims, typ = v[key]["dims"], v[key]["type"]
    return {"data": acc, "dims": dims, "type": typ}


def label(names, weights):
    return "+".join(f"{n}*{round(w, 3)}" for n, w in zip(names, weights))


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("presets", nargs="+",
                   help="timbre (style_ttl), ex: M1:0.5 M5:0.5 ; sert aussi de "
                        "rythme si --dp absent")
    p.add_argument("--dp", nargs="+", default=None,
                   help="rythme (style_dp) separe, ex: --dp M1:1.0 (rythme d'Alex)")
    p.add_argument("-o", "--out", default="voices/CUSTOM.json",
                   help="chemin du JSON de sortie")
    p.add_argument("--styles-dir", default=str(DEFAULT_STYLES_DIR),
                   help=f"dossier des presets (defaut: {DEFAULT_STYLES_DIR})")
    args = p.parse_args()

    styles_dir = Path(args.styles_dir)
    if not styles_dir.exists():
        raise SystemExit(
            f"Dossier de presets introuvable : {styles_dir}\n"
            f"Lance d'abord un run (le modele + presets se telechargent tout seuls)."
        )

    ttl_names, ttl_w = parse_specs(args.presets)
    dp_names, dp_w = parse_specs(args.dp) if args.dp else (ttl_names, ttl_w)

    result = {
        "style_ttl": blend_field(styles_dir, "style_ttl", ttl_names, ttl_w),
        "style_dp": blend_field(styles_dir, "style_dp", dp_names, dp_w),
        "metadata": {
            "source_file": f"ttl[{label(ttl_names, ttl_w)}] dp[{label(dp_names, dp_w)}]",
            "source_sample_rate": 44100,
            "target_sample_rate": 44100,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result), encoding="utf-8")
    print(f"OK -> {out}")
    print(f"     timbre : {label(ttl_names, ttl_w)}")
    print(f"     rythme : {label(dp_names, dp_w)}")


if __name__ == "__main__":
    main()
