from proofagent_harness.per_export import _public_model, evaluator_models


def test_routing_prefix_dropped_so_the_record_names_the_model():
    assert _public_model("lm_studio/google/gemma-3-12b") == "google/gemma-3-12b"
    assert _public_model("gpt-4o-2024-08-06") == "gpt-4o-2024-08-06"
    assert _public_model(None) is None
    rows = evaluator_models({"primary_llm_model": "lm_studio/google/gemma-3-12b"})
    assert {r["model"] for r in rows} == {"google/gemma-3-12b"}
