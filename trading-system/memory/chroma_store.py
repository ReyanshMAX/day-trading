"""ChromaDB persistent store for regime classification outcomes.

Single responsibility: store and retrieve past LLM regime classifications
for few-shot prompt injection.
"""

import logging
from pathlib import Path

import chromadb

log = logging.getLogger(__name__)

_DB_PATH = str(Path(__file__).parent.parent / "chroma_db")


class ChromaStore:
    """Persistent vector store for regime outcome memory."""

    def __init__(self, path: str = _DB_PATH, min_outcomes_for_summary: int = 5) -> None:
        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection("regime_outcomes")
        self._min_outcomes_for_summary = min_outcomes_for_summary

    def store_classification(
        self,
        ticker: str,
        regime_state,
        headlines: list[str],
        signal_score: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        """Upsert a classification record. ID is ticker_date.

        signal_score and confidence default to 0.0 because the classifier
        fires before any trade signal is computed — they are reserved for
        future use when the executor updates the record post-trade.
        """
        from datetime import date
        doc_id = f"{ticker}_{date.today().isoformat()}"
        headlines_summary = " | ".join(headlines[:5]) if headlines else "no headlines"
        document = f"{ticker} | {regime_state.catalyst} | {headlines_summary}"
        metadata = {
            "ticker": ticker,
            "regime": regime_state.regime,
            "direction": regime_state.direction,
            "conviction": regime_state.conviction,
            "outcome": "pending",
            "pnl_pct": 0.0,
            "date": date.today().isoformat(),
            "catalyst": regime_state.catalyst,
            "signal_score": signal_score,
            "confidence": confidence,
        }
        self._collection.upsert(ids=[doc_id], documents=[document], metadatas=[metadata])
        log.debug("Stored classification for %s", ticker)

    def update_outcome(self, ticker: str, date_str: str, pnl_pct: float) -> None:
        """Update outcome and P&L when a position closes."""
        doc_id = f"{ticker}_{date_str}"
        try:
            result = self._collection.get(ids=[doc_id])
            if not result["metadatas"]:
                return
            meta = dict(result["metadatas"][0])
            meta["pnl_pct"] = pnl_pct
            meta["outcome"] = "profitable" if pnl_pct > 0 else "unprofitable"
            self._collection.update(ids=[doc_id], metadatas=[meta])
            log.debug("Updated outcome for %s: %.2f%%", ticker, pnl_pct)
        except Exception as e:
            log.error("update_outcome failed for %s: %s", ticker, e)

    def get_similar_contexts(self, ticker: str, headlines: list[str], n: int = 2) -> list[str]:
        """Semantic search for past similar regimes. Returns formatted strings.

        Always filters by ticker. If the ticker has 5 or more completed
        outcomes (pnl_pct != 0 or outcome != 'pending'), appends a
        performance summary line to each result.
        """
        query = " ".join(headlines[:5]) if headlines else ticker
        try:
            count = self._collection.count()
            if count == 0:
                return []

            # --- Performance summary: query all completed outcomes for ticker ---
            perf_summary: str | None = None
            try:
                all_ticker_results = self._collection.get(
                    where={"ticker": ticker},
                )
                all_metas = all_ticker_results.get("metadatas") or []
                completed = [
                    m for m in all_metas
                    if m.get("outcome", "pending") != "pending"
                ]
                if len(completed) >= self._min_outcomes_for_summary:
                    pnl_values = [float(m.get("pnl_pct", 0.0)) for m in completed]
                    wins = sum(1 for p in pnl_values if p > 0)
                    win_rate = wins / len(pnl_values) * 100
                    avg_pnl = sum(pnl_values) / len(pnl_values)
                    perf_summary = (
                        f"Historical performance for {ticker}: {len(completed)} trades, "
                        f"win rate {win_rate:.0f}%, avg pnl {avg_pnl:+.2f}%."
                    )
            except Exception as perf_err:
                log.debug("get_similar_contexts: perf summary failed for %s: %s", ticker, perf_err)

            # --- Semantic query, always filtered to this ticker ---
            results = self._collection.query(
                query_texts=[query],
                n_results=min(n, count),
                where={"ticker": ticker},
            )
            formatted = []
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                entry = (
                    f"{meta['ticker']} {meta['date']}: {meta['regime']} cv={meta['conviction']}"
                    f" | {meta.get('catalyst','')} | {meta['outcome']} ({meta['pnl_pct']:+.2f}%)"
                )
                if perf_summary:
                    entry += f" | {perf_summary}"
                formatted.append(entry)
            return formatted
        except Exception as e:
            log.warning("get_similar_contexts failed: %s", e)
            return []
