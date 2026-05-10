from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class RAGEvaluationSample:
    question: str
    answer: str
    contexts: list[str]
    metadata: dict[str, Any] | None = None


class EvaluationService:
    """Sidecar RAG answer evaluator.

    Preferred backend is `llm`, using evaluator_agent as LLM-as-Judge.
    `custom` is a deterministic fallback only; it is not a faithful RAGAS
    replacement and should not be treated as a formal quality score.
    """

    def __init__(self, backend: str | None = None) -> None:
        self.backend = (backend or settings.eval_backend or "llm").lower()

    async def evaluate_retrieval(
        self, question: str, contexts: list[str]
    ) -> dict[str, Any] | None:
        """Evaluate retrieval quality (context_precision) without a generated answer."""
        if not settings.eval_enabled or settings.eval_mode == "off":
            return None
        if len(contexts) < settings.eval_min_contexts:
            return None
        if not question.strip():
            return None

        if self.backend == "llm":
            try:
                return await self._run_llm_retrieval_judge(question, contexts)
            except Exception as exc:
                logger.warning("LLM retrieval evaluation failed, falling back to custom: %s", exc)

        question_terms = _terms(question)
        raw_texts = [c.split("\n", 1)[-1] for c in contexts]
        context_precision = _context_precision(question_terms, raw_texts)
        return _with_summary(
            {"context_precision": context_precision},
            backend="custom",
            heuristic=True,
        )

    async def _run_llm_retrieval_judge(
        self, question: str, contexts: list[str]
    ) -> dict[str, Any]:
        from app.services.llm import LLMMessage, get_agent_llm_provider, load_agent_llm_config

        cfg = load_agent_llm_config().get("evaluator_agent", {})
        llm = get_agent_llm_provider("evaluator_agent", cfg.get("provider"), cfg.get("model"))
        ctx_text = "\n\n---\n\n".join(contexts[:8])
        resp = await llm.complete_json(
            messages=[LLMMessage(role="user", content=(
                f"Question:\n{question}\n\n"
                f"Retrieved contexts:\n{ctx_text}\n\n"
                "Return only JSON."
            ))],
            system=(
                "You are a strict RAG retrieval evaluator. Score from 0 to 1.\n"
                "context_precision: check whether the retrieved contexts are actually useful "
                "for answering the question. If only some contexts are useful or they contain "
                "much unrelated material, reduce this score.\n"
                "Return only JSON with keys: context_precision, rationale. "
                "rationale must be a short Chinese explanation of the main deductions."
            ),
            temperature=0,
        )
        return _with_summary(
            {"context_precision": _score(resp.get("context_precision"))},
            backend="llm",
            rationale=str(resp.get("rationale") or "").strip(),
        )

    async def run(self, sample: RAGEvaluationSample) -> dict[str, Any] | None:
        if not settings.eval_enabled or settings.eval_mode == "off":
            return None
        if len(sample.contexts) < settings.eval_min_contexts:
            return None
        if not sample.question.strip() or not sample.answer.strip():
            return None

        if self.backend == "llm":
            try:
                return await self._run_llm_judge(sample)
            except Exception as exc:
                logger.warning("LLM evaluation failed, falling back to custom evaluator: %s", exc)

        if self.backend == "ragas":
            try:
                return await self._run_ragas(sample)
            except Exception as exc:
                logger.warning("RAGAS evaluation failed, falling back to custom evaluator: %s", exc)

        return self._run_custom(sample)

    async def _run_ragas(self, sample: RAGEvaluationSample) -> dict[str, Any]:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import answer_relevancy, context_precision, faithfulness

        dataset = Dataset.from_list([{
            "question": sample.question,
            "answer": sample.answer,
            "contexts": sample.contexts,
        }])
        result = evaluate(
            dataset=dataset,
            metrics=[faithfulness, answer_relevancy, context_precision],
        )
        row = result.to_pandas().iloc[0].to_dict()
        return _with_summary({
            "faithfulness": _score(row.get("faithfulness")),
            "answer_relevancy": _score(row.get("answer_relevancy")),
            "context_precision": _score(row.get("context_precision")),
        }, backend="ragas")

    async def _run_llm_judge(self, sample: RAGEvaluationSample) -> dict[str, Any]:
        from app.services.llm import LLMMessage, get_agent_llm_provider, load_agent_llm_config

        cfg = load_agent_llm_config().get("evaluator_agent", {})
        llm = get_agent_llm_provider("evaluator_agent", cfg.get("provider"), cfg.get("model"))
        contexts = "\n\n---\n\n".join(sample.contexts[:8])
        resp = await llm.complete_json(
            messages=[LLMMessage(role="user", content=(
                f"Question:\n{sample.question}\n\n"
                f"Answer:\n{sample.answer}\n\n"
                f"Retrieved contexts:\n{contexts}\n\n"
                "Return only JSON."
            ))],
            system=(
                "You are a strict RAG answer evaluator. Score from 0 to 1.\n"
                "Do not give 1.0 unless the criterion is fully and explicitly satisfied.\n"
                "Use this calibration:\n"
                "- 1.0: perfect, all important claims are directly supported and no material issue exists.\n"
                "- 0.8: good, mostly supported/relevant, but with minor omissions or paraphrase uncertainty.\n"
                "- 0.6: acceptable, partially supported/relevant, but missing important details.\n"
                "- 0.4: weak, only loosely supported/relevant.\n"
                "- 0.2: mostly unsupported/irrelevant.\n"
                "- 0.0: no useful support/relevance.\n"
                "faithfulness: check whether each substantive claim in the answer is supported by the contexts. "
                "If the answer adds claims not present in contexts, reduce this score.\n"
                "answer_relevancy: check whether the answer directly addresses the question. "
                "Generic background, evasive answers, or missing subquestions must reduce this score.\n"
                "context_precision: check whether the retrieved contexts are actually useful for answering the question. "
                "If only some contexts are useful or they contain much unrelated material, reduce this score.\n"
                "Allow paraphrases and translated terminology, but still require evidence in the contexts.\n"
                "Return only JSON with keys: faithfulness, answer_relevancy, context_precision, rationale. "
                "rationale must be a short Chinese explanation of the main deductions."
            ),
            temperature=0,
        )
        return _with_summary({
            "faithfulness": _score(resp.get("faithfulness")),
            "answer_relevancy": _score(resp.get("answer_relevancy")),
            "context_precision": _score(resp.get("context_precision")),
        }, backend="llm", rationale=str(resp.get("rationale") or "").strip())

    def _run_custom(self, sample: RAGEvaluationSample) -> dict[str, Any]:
        question_terms = _terms(sample.question)
        answer_terms = _terms(sample.answer)
        context_terms = _terms("\n".join(sample.contexts))

        # This is only a fallback. Use a softened overlap score to avoid
        # absurdly low values for Chinese paraphrases and translated terms.
        faithfulness = _soft_coverage(answer_terms, context_terms)
        answer_relevancy = max(
            _soft_coverage(question_terms, answer_terms),
            _soft_coverage(answer_terms, question_terms) * 0.75,
        )
        context_precision = _context_precision(question_terms, sample.contexts)

        return _with_summary({
            "faithfulness": faithfulness,
            "answer_relevancy": answer_relevancy,
            "context_precision": context_precision,
        }, backend="custom", heuristic=True)


def build_rag_evaluation_sample(state, reply: str) -> RAGEvaluationSample | None:
    # Prefer split architecture: retrieval_agent owns contexts, reading_agent owns answer
    retrieval_result = state.agent_outputs.get("retrieval_agent", {}).get("result", {})
    read_result = state.agent_outputs.get("reading_agent", {}).get("result", {})

    contexts = retrieval_result.get("contexts") or []
    if not contexts:
        # Fallback: legacy single-agent output
        notes = read_result.get("reading_notes", [])
        contexts = (notes[0].get("contexts") if notes else None) or read_result.get("contexts") or []

    if not contexts:
        return None

    question = retrieval_result.get("question") or state.user_goal
    answer = read_result.get("answer") or reply
    metadata = retrieval_result.get("metadata") or read_result.get("metadata") or {}

    return RAGEvaluationSample(
        question=question,
        answer=answer,
        contexts=[str(c) for c in contexts if str(c).strip()],
        metadata=metadata,
    )


def _terms(text: str) -> set[str]:
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "what", "how", "why", "which", "about", "into", "based", "using", "method",
        "paper", "result", "results", "show", "shows",
    }
    terms = {t for t in latin if t not in stop}
    for chunk in chinese:
        if len(chunk) <= 4:
            terms.add(chunk)
        else:
            terms.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return terms


def _soft_coverage(source: set[str], target: set[str]) -> float:
    if not source:
        return 0.0
    overlap = len(source & target) / len(source)
    if overlap == 0 and target:
        return 0.35
    return _score(0.25 + 0.75 * overlap)


def _context_precision(question_terms: set[str], contexts: list[str]) -> float:
    if not contexts:
        return 0.0
    if not question_terms:
        return 0.5
    relevant = 0
    weighted = 0.0
    for idx, context in enumerate(contexts, start=1):
        ctx_terms = _terms(context)
        if not ctx_terms:
            continue
        overlap = len(question_terms & ctx_terms) / max(len(question_terms), 1)
        if overlap > 0:
            relevant += 1
            weighted += relevant / idx
    return _score(weighted / len(contexts)) if relevant else 0.35


def _with_summary(
    scores: dict[str, float],
    *,
    backend: str,
    heuristic: bool = False,
    rationale: str = "",
) -> dict[str, Any]:
    warning = None
    if heuristic:
        warning = "启发式评测结果，仅作参考；建议配置 evaluator_agent 使用 LLM 评测。"
    elif scores.get("faithfulness", 1.0) < settings.eval_faithfulness_warning_threshold:
        warning = "回答可能缺少上下文支持，请谨慎采信。"
    elif scores.get("answer_relevancy", 1.0) < settings.eval_relevancy_warning_threshold:
        warning = "回答与问题的相关性偏低。"
    elif scores.get("context_precision", 1.0) < settings.eval_precision_warning_threshold:
        warning = "检索到的上下文相关性偏低。"

    confidence = min(scores.values()) if scores else 0.0
    if confidence >= 0.9:
        label = "高可信"
    elif confidence >= 0.7:
        label = "基本可信"
    elif confidence >= 0.5:
        label = "需复核"
    else:
        label = "高风险"

    return {
        **scores,
        "warning": warning,
        "label": label,
        "backend": backend,
        "heuristic": heuristic,
        "rationale": rationale,
    }


def _score(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return round(max(0.0, min(1.0, v)), 3)
