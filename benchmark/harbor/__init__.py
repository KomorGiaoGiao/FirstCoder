"""Harbor benchmark adapters for FirstCoder.

Harbor is an optional, external benchmark runtime.  Keeping its adapters in
``benchmark.harbor`` prevents Harbor's dependency and container lifecycle from
leaking into FirstCoder's core runtime. Dataset-specific integrations live in
``aider_polyglot`` and ``terminal_bench``; reusable runtime helpers live in
``shared``.
"""
