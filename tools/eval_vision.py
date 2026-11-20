#!/usr/bin/env python3
"""
Accuracy check for Photo Quote extraction on real photos.

    export ANTHROPIC_API_KEY=...
    python3 tools/eval_vision.py sheet.jpg                    # vs the brief's sample ground truth
    python3 tools/eval_vision.py a.jpg b.jpg --truth truth.json
    python3 tools/eval_vision.py sheet.jpg --runs 3           # same photo 3x: is it stable?

Each run is one paid API call (roughly a few cents per photo). Numbers must
match exactly; text fields pass when they are close (wording may differ).
Exit code 0 only if every numeric field matched on every run.
"""
import argparse
import json
import os
import sys
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from app.vision import extractor                         # noqa: E402
from app.vision.schema import SAMPLE_GROUND_TRUTH        # noqa: E402

TEXT_PASS = 0.75


def judge(field, expected, got):
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return ('ok' if got == expected else 'FAIL'), True
    if expected is None:
        return ('ok' if got in (None, '') else 'FAIL'), False
    a, b = str(expected).lower(), str(got or '').lower()
    r = SequenceMatcher(None, a, b).ratio()
    return ('ok' if r >= 0.95 else '~' if r >= TEXT_PASS else 'FAIL') + f' ({r:.2f})', False


def main():
    p = argparse.ArgumentParser()
    p.add_argument('images', nargs='+')
    p.add_argument('--truth', help='ground-truth JSON (default: sample sheet from the brief)')
    p.add_argument('--runs', type=int, default=1)
    p.add_argument('--no-preprocess', action='store_true')
    p.add_argument('--save', help='folder to save each raw result JSON')
    args = p.parse_args()

    ok, reason = extractor.available()
    if not ok:
        sys.exit(reason)
    truth = json.load(open(args.truth)) if args.truth else SAMPLE_GROUND_TRUTH
    all_numeric_ok = True
    for path in args.images:
        data = open(path, 'rb').read()
        for run in range(1, args.runs + 1):
            print(f'\n=== {path}  run {run}/{args.runs} ===')
            try:
                res = extractor.extract(data, do_preprocess=not args.no_preprocess)
            except extractor.ExtractionError as e:
                print('ERROR:', e.message)
                all_numeric_ok = False
                continue
            print(f'model {res.model} · attempts {res.attempts} · tokens in/out '
                  f'{res.usage.get("input_tokens")}/{res.usage.get("output_tokens")}')
            for field, exp, got, _ in extractor.compare(res.data, truth):
                verdict, numeric = judge(field, exp, got)
                if numeric and not verdict.startswith('ok'):
                    all_numeric_ok = False
                print(f'  {verdict:<11} {field:<26} expected={exp!r:<48} got={got!r}')
            if res.data['needs_review']:
                print('  needs_review:')
                for r in res.data['needs_review']:
                    print(f'    - {r["field"]}: {r["reason"]}')
            if args.save:
                os.makedirs(args.save, exist_ok=True)
                out = os.path.join(args.save, f'{os.path.basename(path)}.run{run}.json')
                json.dump({'data': res.data, 'raw': res.raw_text}, open(out, 'w'), indent=2)
    print('\nNUMERIC FIELDS:', 'ALL MATCH' if all_numeric_ok else 'MISMATCHES — see FAIL rows')
    return 0 if all_numeric_ok else 1


if __name__ == '__main__':
    sys.exit(main())
