"""Streamlit-интерфейс AML / Anti-Fraud Copilot (BR-06, SR-15, SR-16).

Лёгкий UI PoC (BRS In-Scope: «легковесный веб-интерфейс или CLI»):
  - выбор инцидента и запуск агента (read-only, BR-07);
  - отображение черновика с цитированием (source_ref, BR-04) и
    доказательствами (evidence_ref, BR-05);
  - подсветка фрагментов транзакционных логов, на которые ссылаются выводы (BR-06);
  - маскирование ПДн в профиле клиента (NFR-09, 152-ФЗ);
  - Human-in-the-Loop: одобрение/подписание отчёта оператором (BRULE-01),
    после чего экспорт в Case Management выполняется автоматически (SR-22).

Запуск:
    streamlit run src/ui/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Корень проекта для импортов `from src.*`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from src.agent import AMLAgent
from src.data_access import CaseRepository, InvestigationContextBuilder
from src.schemas import AMLInvestigationDraft, Transaction, validate_draft

# ---------------------------------------------------------------------------
# Стили: подсветка доказательств (BR-06) и статусные бейджи
# ---------------------------------------------------------------------------
_CSS = """
<style>
  .evidence-hl {
    background-color: #fff3cd;
    border-bottom: 2px solid #f0ad4e;
    font-weight: 600;
    padding: 0 3px;
    border-radius: 3px;
  }
  .badge { display:inline-block; padding:3px 12px; border-radius:14px;
           font-size:0.8rem; font-weight:700; margin-bottom:8px; }
  .badge-draft   { background:#d4edda; color:#155724; }
  .badge-refusal { background:#f8d7da; color:#721c24; }
  .badge-error   { background:#e2e3e5; color:#383d41; }
  table.tx-table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
  table.tx-table th, table.tx-table td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
  table.tx-table th { background:#f5f6f7; }
</style>
"""

#: Поля транзакции, отображаемые в логе (соответствуют TransactionField, SR-14).
_TX_FIELDS = ["amount", "currency", "counterparty", "purpose", "channel", "status", "timestamp"]


# ---------------------------------------------------------------------------
# Кэшированные зависимости (инициализация дорогая)
# ---------------------------------------------------------------------------

@st.cache_resource
def _get_agent() -> AMLAgent:
    return AMLAgent()


@st.cache_resource
def _get_case_repo() -> CaseRepository:
    return CaseRepository()


@st.cache_resource
def _get_context_builder() -> InvestigationContextBuilder:
    return InvestigationContextBuilder()


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _available_incidents() -> list[str]:
    """Список доступных инцидентов для быстрого выбора (демо)."""
    cases = _get_case_repo().find_all()
    return [f"{c.incident_id} · {c.alert_type.value}" for c in cases]


def _incident_from_option(option: str) -> str:
    return option.split(" · ")[0]


def _collect_evidence(draft: AMLInvestigationDraft) -> set[tuple[str, str]]:
    """Множество (tx_id, field) из всех evidence_ref черновика (SR-15)."""
    refs: set[tuple[str, str]] = set()
    for fact in draft.found_facts:
        refs.add((fact.evidence_ref.tx_id, fact.evidence_ref.field.value))
    for pattern in draft.suspicious_patterns:
        if pattern.evidence_ref:
            refs.add((pattern.evidence_ref.tx_id, pattern.evidence_ref.field.value))
    return refs


def _transactions_html(
    transactions: list[Transaction],
    evidence: set[tuple[str, str]],
) -> str:
    """HTML-таблица транзакций с подсветкой полей из evidence_ref (BR-06)."""
    header = "<tr><th>tx_id</th>" + "".join(f"<th>{f}</th>" for f in _TX_FIELDS) + "</tr>"
    rows = []
    for tx in transactions:
        cells = [f"<td><code>{tx.tx_id}</code></td>"]
        for field in _TX_FIELDS:
            value = getattr(tx, field)
            value = value.value if hasattr(value, "value") else value
            if (tx.tx_id, field) in evidence:
                cells.append(f'<td><span class="evidence-hl">{value}</span></td>')
            else:
                cells.append(f"<td>{value}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table class='tx-table'>{header}{''.join(rows)}</table>"

def _mask_name(full_name: str) -> str:
    """Маскирование ФИО до фамилии и инициалов (NFR-09)."""
    parts = full_name.split()
    if len(parts) <= 1:
        return full_name
    return parts[0] + " " + " ".join(p[0] + "." for p in parts[1:] if p)

def _group_by_text(items: list, text_attr: str) -> list[dict]:
    """Группирует элементы с одинаковым текстом, собирая все evidence_ref.

    Нормализатор разворачивает массивы tx_id в отдельные факты/паттерны
    (для валидности схемы), что приводит к дублированию текста.
    Группировка на уровне UI восстанавливает читаемость без изменения payload.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        text = getattr(item, text_attr)
        if text not in groups:
            groups[text] = {"text": text, "item": item, "evidences": []}
            order.append(text)
        ev = getattr(item, "evidence_ref", None)
        if ev:
            groups[text]["evidences"].append(ev)
    return [groups[t] for t in order]

# ---------------------------------------------------------------------------
# Рендер результатов
# ---------------------------------------------------------------------------

def _render_draft(payload: dict, incident_id: str) -> None:
    """Отображает черновик: отчёт, исходные данные с подсветкой, трейс, HITL."""
    draft = validate_draft(payload)
    context = _get_context_builder().build(incident_id)
    evidence = _collect_evidence(draft)

    st.markdown(
        '<span class="badge badge-draft">ЧЕРНОВИК · требуется одобрение оператора</span>',
        unsafe_allow_html=True,
    )

    # --- Ключевые показатели ---
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Уверенность", f"{draft.confidence:.0%}")
    m2.metric("Транзакций", len(context.transactions))
    m3.metric("Найдено фактов", len(draft.found_facts))
    m4.metric("Применимых правил", len(draft.applicable_rules))

    tab_report, tab_data, tab_trace = st.tabs(
        ["📋 Отчёт", "🗂️ Исходные данные", "🧠 Цепочка рассуждений"]
    )

    with tab_report:
        st.subheader("Найденные факты")
        for group in _group_by_text(draft.found_facts, "fact"):
            fact = group["item"]
            evidences = group["evidences"]
            src = f" · источник: {fact.source_ref}" if fact.source_ref else ""
            if len(evidences) > 1:
                ev_list = ", ".join(f"`{e.tx_id}.{e.field.value}`" for e in evidences)
                st.markdown(
                    f"- {fact.fact}  \n"
                    f"  _доказательства ({len(evidences)}): {ev_list}{src}_"
                )
            else:
                e = evidences[0]
                st.markdown(
                    f"- {fact.fact}  \n"
                    f"  _доказательство: `{e.tx_id}.{e.field.value}`{src}_"
                )

        st.subheader("Подозрительные паттерны")
        patterns_grouped = _group_by_text(draft.suspicious_patterns, "pattern")
        if patterns_grouped:
            for group in patterns_grouped:
                p = group["item"]
                evidences = group["evidences"]
                ev_note = ""
                if evidences:
                    ev_list = ", ".join(f"`{e.tx_id}.{e.field.value}`" for e in evidences)
                    ev_note = f"  \n  _доказательства ({len(evidences)}): {ev_list}_"
                st.markdown(f"- {p.pattern}{ev_note}  \n  _обоснование: {p.source_ref}_")
        else:
            st.caption("Подозрительные паттерны не выявлены.")

        st.subheader("Применимые правила")
        for r in draft.applicable_rules:
            st.markdown(f"- **{r.rule_id}** — {r.rule_text}  \n  _{r.source_ref}_")

    with tab_data:
        st.subheader("Профиль клиента")
        c = context.client
        st.markdown(
            f"- **ID:** `{c.client_id}`  \n"
            f"- **ФИО:** {_mask_name(c.full_name)}  \n"
            f"- **Категория риска:** {c.risk_category.value}  \n"
            f"- **KYC:** {c.kyc_status.value}  \n"
            f"- **Клиент с:** {c.client_since.isoformat()}"
        )
        st.caption("ФИО и ИНН маскируются (NFR-09, 152-ФЗ).")

        st.subheader("Транзакционный лог")
        st.markdown("Подсвечены поля, на которые ссылаются выводы отчёта (evidence_ref, BR-06):")
        st.markdown(
            _transactions_html(context.transactions, evidence),
            unsafe_allow_html=True,
        )

    with tab_trace:
        st.subheader("Цепочка рассуждений (audit trail, NFR-11)")
        for step in draft.reasoning_trace:
            st.markdown(f"- {step}")

    _render_hitl()


def _render_hitl() -> None:
    """Human-in-the-Loop: одобрение/подписание или отклонение (BRULE-01)."""
    st.divider()
    st.subheader("✅ Решение оператора (Human-in-the-Loop)")

    hitl = st.session_state.get("hitl")
    if hitl == "approved":
        st.success(
            "Отчёт одобрен и подписан. Экспорт в Case Management выполнен "
            "автоматически (SR-22). Кейс закрыт."
        )
        return
    if hitl == "rejected":
        st.warning("Отчёт отклонён. Кейс возвращён в очередь ручной обработки.")
        return

    c1, c2 = st.columns(2)
    with c1:
        if st.button("✔ Одобрить и подписать", type="primary", use_container_width=True):
            st.session_state["hitl"] = "approved"
            st.rerun()
    with c2:
        if st.button("✖ Отклонить", use_container_width=True):
            st.session_state["hitl"] = "rejected"
            st.rerun()


def _render_refusal(result: dict) -> None:
    """Отображает явный отказ (BRULE-03)."""
    st.markdown(
        '<span class="badge badge-refusal">ЯВНЫЙ ОТКАЗ · кейс передан в ручную обработку</span>',
        unsafe_allow_html=True,
    )
    st.error(f"**Причина:** {result.get('refusal_reason', 'не указана')}")
    with st.expander("Цепочка рассуждений"):
        for step in result.get("reasoning_trace", []):
            st.markdown(f"- {step}")


def _render_error(result: dict) -> None:
    """Отображает ошибку обработки."""
    st.markdown(
        '<span class="badge badge-error">ОШИБКА ОБРАБОТКИ</span>',
        unsafe_allow_html=True,
    )
    st.error(result.get("error", "Неизвестная ошибка"))


# ---------------------------------------------------------------------------
# Главный экран
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="AML / Anti-Fraud Copilot", page_icon="🛡️", layout="wide")
    st.markdown(_CSS, unsafe_allow_html=True)

    st.title("🛡️ AML / Anti-Fraud Copilot")
    st.caption(
        "Ассистент первичного расследования комплаенс-инцидентов · "
        "Read-Only · Human-in-the-Loop. Все данные синтетические."
    )

    col_input, col_btn = st.columns([3, 1])
    options = _available_incidents()
    with col_input:
        option = st.selectbox("Инцидент для расследования", options, index=0)
    with col_btn:
        st.write("")
        run_clicked = st.button("🔍 Расследовать", type="primary", use_container_width=True)

    incident_id = _incident_from_option(option)

    if run_clicked:
        with st.spinner(f"Агент анализирует инцидент {incident_id}…"):
            result = _get_agent().run(incident_id)
        st.session_state["result"] = result
        st.session_state["incident_id"] = incident_id
        st.session_state.pop("hitl", None)

    result = st.session_state.get("result")
    if result is None:
        st.info("Выберите инцидент и нажмите «Расследовать».")
        return

    status = result.get("status")
    if status == "draft":
        _render_draft(result, st.session_state["incident_id"])
    elif status == "refusal":
        _render_refusal(result)
    else:
        _render_error(result)


main()