# Fixture provenance

- `smoke-lite-10.json` contains the ten complete test rows selected by
  `../subsets/smoke-lite-10.json` from
  [princeton-nlp/SWE-bench_Lite at revision 6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/tree/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2).
  The source `data/test-00000-of-00001.parquet` was downloaded at that immutable
  revision and verified against its LFS SHA-256 before extracting the rows.
  The source and extracted fixture hashes are recorded in `../pins.json`.
- `sympy-matexpr.base.txt` is the unmodified
  [sympy/matrices/expressions/matexpr.py](https://github.com/sympy/sympy/blob/479939f8c65c8c2908bbedc959549a257a7c0b0b/sympy/matrices/expressions/matexpr.py)
  from SymPy commit `479939f8c65c8c2908bbedc959549a257a7c0b0b`. It enables the
  offline HTTP integration test and is checked against the pinned source hash.
- `SYMPY-LICENSE.txt` is SymPy's license from that same commit.

The gold patch is deliberately used as a scripted tool/harness fixture. These
inputs must never be described as unseen tasks solved autonomously by a model.
Docker digests were read from the official `swebench` Docker Hub namespace;
`pins.json` records each exact linux/amd64 manifest and original tag.
