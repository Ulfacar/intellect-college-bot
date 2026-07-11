#!/usr/bin/env python3
"""Run scripted quality scenarios through the real bot orchestrator.

The script forces in-memory state/CRM/panel before importing app modules, so it
can be run locally or inside the VPS container without writing simulation data
to production storage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

os.environ["CRM_BACKEND"] = "stub"
os.environ["STATE_BACKEND"] = "memory"
os.environ["PANEL_BACKEND"] = "memory"
os.environ["DEBOUNCE_SECONDS"] = "0"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import validator
from app.agent.llm import client
from app.channels.base import Message
from app.config import BotConfig, settings
from app.core import flags
from app.core.orchestrator import Orchestrator
from app.core.state import get_state_store


DEFAULT_SCENARIOS = ROOT / "scenarios"
DEFAULT_RUNS = ROOT / "runs"


@dataclass
class JudgeResult:
    passed: bool
    score: int
    failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)
    recommendation: str = ""
    source: str = "rule"
    raw: str = ""
    error_type: str = ""


@dataclass
class RunResult:
    scenario_id: str
    run_no: int
    transcript: list[dict[str, str]]
    state: Any
    rule: JudgeResult
    llm: JudgeResult | None
    passed: bool
    score: int
    log_path: Path
    judge_path: Path | None = None


class CollectChannel:
    channel = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def parse(self, raw: dict) -> Message:  # pragma: no cover
        raise NotImplementedError

    async def send(self, chat_id: str, text: str, **kwargs) -> str:
        self.sent.append((chat_id, text))
        return f"sim-{len(self.sent)}"


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    files = [path] if path.is_file() else sorted(path.glob("*.json"))
    scenarios: list[dict[str, Any]] = []
    for file in files:
        with file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            scenarios.extend(data)
        else:
            scenarios.append(data)
    return scenarios


async def run_dialog(scn: dict[str, Any], run_no: int, out_dir: Path) -> RunResult:
    bot_name = scn.get("bot", "admission")
    bot = BotConfig(id=f"sim_{bot_name}", scenario="admission")
    user_id = f"sim-{scn['id']}-{run_no}-{datetime.now(timezone.utc).timestamp()}"
    channel = CollectChannel()
    orch = Orchestrator(channel=channel, bot=bot)
    transcript: list[dict[str, str]] = []

    for user_text in scn["messages"]:
        transcript.append({"sender": "client", "text": user_text})
        before = len(channel.sent)
        msg = Message(channel="telegram", user_id=user_id, chat_id=user_id, text=user_text)
        await orch.handle(msg)
        for _, bot_text in channel.sent[before:]:
            transcript.append({"sender": "bot", "text": bot_text})

    state = await get_state_store().load(f"{bot.id}:{user_id}")
    rule = rule_judge(scn, transcript, state)
    log_path = out_dir / f"{scn['id']}.run{run_no}.txt"
    write_transcript(log_path, scn, run_no, transcript, state, rule)
    return RunResult(
        scenario_id=scn["id"],
        run_no=run_no,
        transcript=transcript,
        state=state,
        rule=rule,
        llm=None,
        passed=rule.passed,
        score=rule.score,
        log_path=log_path,
    )


def bot_text(transcript: list[dict[str, str]]) -> str:
    return "\n".join(item["text"] for item in transcript if item["sender"] == "bot")


def rule_judge(scn: dict[str, Any], transcript: list[dict[str, str]], state: Any) -> JudgeResult:
    """Двухуровневые правила.

    HARD (блокируют pass) — только механика, где regex не ошибётся: пустой/ошибочный
    ответ. SOFT (advisory) — семантика качества квалификации/эскалации, поэтому
    решение отдаём LLM-судье. Когда судья выключен — soft тоже учитывается как провал.
    """
    text = bot_text(transcript)
    client_text = "\n".join(item["text"] for item in transcript if item["sender"] == "client")
    hard: list[str] = []
    soft: list[str] = []

    if not text.strip():
        hard.append("bot_failed: bot returned no replies")
    if re.search(r"секундочк|уточню детали и вернусь|temporarily unavailable|traceback|exception", text, re.I):
        hard.append("bot_failed: fallback/error-like reply")

    for reply in [item["text"] for item in transcript if item["sender"] == "bot"]:
        _, violations = validator.validate_reply(reply, "admission")
        for violation in violations:
            if violation in {
                "admission_guarantee",
                "admission_price_mismatch",
                "admission_discount_amount",
                "admission_passing_score",
                "admission_duration_claim",
            }:
                soft.append(f"soft: validator flagged {violation}")

    qual = getattr(state, "qualification", {}) or {}
    if len(scn.get("messages", [])) >= 3 and scn.get("bot") == "admission" and len(qual) < 2:
        soft.append("soft: weak admission qualification state")

    score = max(0, 10 - len(hard) * 3 - len(soft))
    return JudgeResult(passed=not hard, score=score, failures=hard, soft_failures=soft, source="rule")


async def llm_judge(scn: dict[str, Any], run: RunResult, judge_model: str) -> JudgeResult:
    if not settings.openrouter_api_key:
        return JudgeResult(
            passed=False,
            score=0,
            failures=["judge_error: OPENROUTER_API_KEY is not configured"],
            source="llm",
            error_type="judge_error",
        )

    transcript = format_transcript(run.transcript)
    system = (
        "Ты строгий QA-судья для AI-бота приёмной комиссии Intellect College. "
        "Оценивай только по фактам диалога и чек-листу. Верни только JSON без markdown. "
        "Схема: {\"passed\": boolean, \"score\": 0-10, \"failures\": [string], "
        "\"recommendation\": string}. Если бот нарушил must_not, passed=false."
    )
    content = {
        "scenario": {
            "id": scn["id"],
            "title": scn["title"],
            "bot": scn["bot"],
            "must": scn.get("must", []),
            "must_not": scn.get("must_not", []),
        },
        "transcript": transcript,
        "qualification_state": getattr(run.state, "qualification", {}),
        "stage": getattr(run.state, "stage", ""),
    }
    try:
        resp = await client().messages.create(
            model=judge_model,
            max_tokens=512,
            system=system,
            tools=[],
            messages=[{"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
        )
        raw = "".join(block.text or "" for block in resp.content if block.type == "text")
        data = parse_json_object(raw)
        return JudgeResult(
            passed=bool(data.get("passed")),
            score=int(data.get("score", 0)),
            failures=[str(x) for x in data.get("failures", [])],
            recommendation=str(data.get("recommendation", "")),
            source="llm",
            raw=raw,
        )
    except Exception as exc:  # noqa: BLE001 - judge failures must be visible in report
        return JudgeResult(
            passed=False,
            score=0,
            failures=[f"judge_error: {type(exc).__name__}: {exc}"],
            source="llm",
            raw=str(exc),
            error_type="judge_error",
        )


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def write_transcript(path: Path, scn: dict[str, Any], run_no: int, transcript: list[dict[str, str]], state: Any, rule: JudgeResult) -> None:
    lines = [
        f"Scenario: {scn['id']} - {scn['title']}",
        f"Bot: {scn['bot']}",
        f"Run: {run_no}",
        f"Stage: {getattr(state, 'stage', '')}",
        f"Qualification: {json.dumps(getattr(state, 'qualification', {}), ensure_ascii=False)}",
        "",
        "Transcript:",
        format_transcript(transcript),
        "",
        f"Rule judge: passed={rule.passed} score={rule.score}",
        *[f"- {failure}" for failure in rule.failures],
        *[f"- {failure}" for failure in rule.soft_failures],
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_judge(path: Path, rule: JudgeResult, llm: JudgeResult | None, passed: bool, score: int) -> None:
    data = {
        "passed": passed,
        "score": score,
        "rule": rule.__dict__,
        "llm": llm.__dict__ if llm else None,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def format_transcript(transcript: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['sender']}: {item['text']}" for item in transcript)


def classify(passed_runs: int, repeats: int) -> str:
    if passed_runs == repeats:
        return "STABLE_GREEN"
    if passed_runs == 0:
        return "FAILED"
    return "FLAKY"


def write_report(out_dir: Path, scenarios: list[dict[str, Any]], results: dict[str, list[RunResult]], args: argparse.Namespace) -> Path:
    stable = flaky = failed = 0
    rows = []
    for scn in scenarios:
        runs = results[scn["id"]]
        passed_runs = sum(1 for r in runs if r.passed)
        status = classify(passed_runs, len(runs))
        stable += status == "STABLE_GREEN"
        flaky += status == "FLAKY"
        failed += status == "FAILED"
        avg = mean(r.score for r in runs) if runs else 0
        rows.append((status, passed_runs, len(runs), avg, scn, runs))

    severity = {"FAILED": 0, "FLAKY": 1, "STABLE_GREEN": 2}
    rows.sort(key=lambda x: (severity[x[0]], x[1], -len(x[4].get("must_not", []))))

    report = out_dir / "report.md"
    lines = [
        f"# Quality report ({len(scenarios)} scenarios x {args.repeats} repeats)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Bot model: `{settings.llm_model_main}`",
        f"Judge model: `{args.judge_model}`",
        f"LLM judge: `{not args.no_llm_judge}`",
        f"OpenRouter key present: `{bool(settings.openrouter_api_key)}`",
        "",
        f"Stable green: {stable}/{len(scenarios)}",
        f"Flaky: {flaky}/{len(scenarios)}",
        f"Failed: {failed}/{len(scenarios)}",
        "",
        "## Failed / Flaky First",
        "",
    ]
    for status, passed_runs, total, avg, scn, runs in rows:
        if status == "STABLE_GREEN":
            continue
        lines.extend(format_report_item(status, passed_runs, total, avg, scn, runs, out_dir))

    lines.extend(["", "## Stable Green", ""])
    for status, passed_runs, total, avg, scn, runs in rows:
        if status == "STABLE_GREEN":
            lines.append(f"- [STABLE_GREEN {passed_runs}/{total}] `{scn['id']}` - avg {avg:.1f}/10")

    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def format_report_item(status: str, passed_runs: int, total: int, avg: float, scn: dict[str, Any], runs: list[RunResult], out_dir: Path) -> list[str]:
    failures: list[str] = []
    for run in runs:
        for failure in run.rule.failures:
            failures.append(f"run{run.run_no}: {failure}")
        for failure in run.rule.soft_failures:
            failures.append(f"run{run.run_no}: {failure}")
        if run.llm:
            for failure in run.llm.failures:
                failures.append(f"run{run.run_no}: llm: {failure}")
    unique = list(dict.fromkeys(failures))[:6]
    lines = [
        f"- [{status} {passed_runs}/{total}] `{scn['id']}` - {scn['title']} - avg {avg:.1f}/10",
    ]
    if unique:
        lines.append(f"  Reasons: {'; '.join(unique)}")
    for run in runs:
        rel = run.log_path.relative_to(out_dir)
        verdict = "PASS" if run.passed else "FAIL"
        lines.append(f"  - run{run.run_no}: {verdict}, score {run.score}/10, log `{rel}`")
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run Intellect College bot quality scenarios.")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS), help="Scenario JSON file or directory.")
    parser.add_argument("--repeats", type=int, default=3, help="Runs per scenario.")
    parser.add_argument("--only", help="Run one scenario id.")
    parser.add_argument("--bot", choices=["admission"], help="Filter by bot/funnel.")
    parser.add_argument("--no-llm-judge", action="store_true", help="Disable LLM judge and use rule-based checks only.")
    parser.add_argument("--judge-model", default=settings.llm_model_cheap,
                        help="LLM judge model. Example: anthropic/claude-sonnet-4.6")
    parser.add_argument("--out", default=str(DEFAULT_RUNS), help="Output directory root.")
    args = parser.parse_args()

    scenarios = load_scenarios(Path(args.scenarios))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] == args.only]
    if args.bot:
        scenarios = [s for s in scenarios if s["bot"] == args.bot]
    if not scenarios:
        raise SystemExit("No scenarios matched.")

    flags.reset()
    await flags.set_flag("bots_enabled", True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results: dict[str, list[RunResult]] = {}
    for scn in scenarios:
        runs: list[RunResult] = []
        for run_no in range(1, args.repeats + 1):
            run = await run_dialog(scn, run_no, out_dir)
            if not args.no_llm_judge:
                # Судья включён: правила гейтят только механику (hard), семантику решает LLM.
                run.llm = await llm_judge(scn, run, args.judge_model)
                run.passed = run.rule.passed and run.llm.passed
                run.score = run.llm.score
            else:
                # Без судьи семантику оценить нечем — soft-правила тоже учитываем как провал.
                run.passed = run.rule.passed and not run.rule.soft_failures
                run.score = run.rule.score
            judge_path = out_dir / f"{scn['id']}.run{run_no}.judge.json"
            write_judge(judge_path, run.rule, run.llm, run.passed, run.score)
            run.judge_path = judge_path
            runs.append(run)
            print(f"{scn['id']} run{run_no}: {'PASS' if run.passed else 'FAIL'} score={run.score}/10")
        all_results[scn["id"]] = runs

    report = write_report(out_dir, scenarios, all_results, args)
    print(f"\nReport: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
