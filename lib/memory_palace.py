"""
Prediction Market Memory Palace — persistent knowledge across sessions.

Adapted from traderbot's ticker-based palace to category-based wings
for prediction markets. Same architecture, different domain.

Architecture (from github.com/milla-jovovich/mempalace):
    Wings   = Categories (politics, economics, weather, crypto, sports, ai_tech, ...)
              + Strategy wing + Market wing
    Halls   = Memory types (facts, events, discoveries, preferences, advice)
    Rooms   = Specific topics ("politics-elections", "weather-temperature", ...)
    Drawers = Verbatim trade reasoning and market observations

Knowledge Graph:
    Temporal entity-relationship triples in SQLite.
    "market_ABC123 → bought_yes → 0.65 (valid_from: 2024-05-01)"
    "politics → win_rate → 0.72 (valid_from: 2024-03-01)"
    "forecaster → calibration → brier_0.18 (valid_from: 2024-04-01)"

Agent Diaries:
    Each governance agent (strategy, risk, compliance, consensus) keeps
    a persistent diary of decisions. Compressed format, append-only.

Security:
    - SQLite with WAL mode for concurrent safety
    - No secrets stored in memories (validated on write)
    - Append-only diaries (forensic-grade)
    - ChromaDB optional — full functionality without it

Requires: pip install chromadb (optional, for semantic search)
Falls back to keyword search if ChromaDB unavailable.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Try ChromaDB for semantic search, fall back gracefully
try:
    import chromadb
    HAS_CHROMA = True
except ImportError:
    HAS_CHROMA = False

PALACE_DIR = Path(__file__).parent.parent / "data" / "palace"
KG_DB = PALACE_DIR / "knowledge_graph.db"
DIARY_DIR = PALACE_DIR / "diaries"

SECRET_KEYWORDS = {"key", "secret", "token", "password", "credential", "api_key"}


# ── Palace Structure ──────────────────────────────────────────────

# Category-based wings for prediction markets
WINGS = {
    # Category wings
    "wing_politics": {"type": "category", "keywords": ["politics", "election", "president", "congress", "vote"]},
    "wing_economics": {"type": "category", "keywords": ["economics", "gdp", "inflation", "fed", "rate", "jobs"]},
    "wing_weather": {"type": "category", "keywords": ["weather", "temperature", "hurricane", "storm", "climate"]},
    "wing_crypto": {"type": "category", "keywords": ["crypto", "bitcoin", "ethereum", "btc", "eth", "defi"]},
    "wing_sports": {"type": "category", "keywords": ["sports", "nfl", "nba", "mlb", "soccer", "game", "match"]},
    "wing_entertainment": {"type": "category", "keywords": ["entertainment", "oscar", "grammy", "movie", "show"]},
    "wing_ai_tech": {"type": "category", "keywords": ["ai", "tech", "gpt", "model", "launch", "benchmark"]},
    "wing_geopolitical": {"type": "category", "keywords": ["geopolitical", "war", "conflict", "sanction", "treaty"]},
    "wing_science": {"type": "category", "keywords": ["science", "discovery", "research", "trial", "fda"]},
    # Strategy & meta wings
    "wing_strategy": {"type": "strategy", "keywords": ["strategy", "kelly", "edge", "calibration", "hermes"]},
    "wing_market": {"type": "market", "keywords": ["market", "platform", "polymarket", "kalshi", "manifold"]},
}

# Halls — same in every wing
HALLS = [
    "hall_facts",        # Decisions locked in: "bought YES on market_ABC at 0.55"
    "hall_events",       # Resolutions/milestones: "market_ABC resolved YES, +$4.50 profit"
    "hall_discoveries",  # Insights: "politics markets overreact to polls by ~5%"
    "hall_preferences",  # Tuning: "prefer markets resolving in 7-14 days"
    "hall_advice",       # Lessons: "don't trade weather markets without NOAA data"
]


def init_palace():
    """Create the palace directory structure. Idempotent."""
    PALACE_DIR.mkdir(parents=True, exist_ok=True)
    DIARY_DIR.mkdir(parents=True, exist_ok=True)

    if HAS_CHROMA:
        client = chromadb.PersistentClient(path=str(PALACE_DIR / "chroma"))
        client.get_or_create_collection(
            name="prediction_drawers",
            metadata={"hnsw:space": "cosine"},
        )

    _init_kg_db()
    return True


# ── Drawers — Verbatim Memory Storage ─────────────────────────────

@dataclass
class Drawer:
    """A single memory unit in the palace."""
    wing: str
    hall: str
    room: str
    content: str
    metadata: dict
    created_at: str = ""
    drawer_id: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.drawer_id:
            h = hashlib.sha256(
                f"{self.wing}:{self.room}:{self.content[:100]}:{self.created_at}".encode()
            ).hexdigest()[:12]
            self.drawer_id = h


def _sanitize_metadata(metadata: dict) -> dict:
    """Strip any field that looks like a secret before storing."""
    sanitized = {}
    for k, v in metadata.items():
        if any(s in k.lower() for s in SECRET_KEYWORDS):
            continue  # Drop secret fields entirely — don't even store redacted
        sanitized[k] = v
    return sanitized


def add_drawer(drawer: Drawer) -> str:
    """Store a memory in the palace. ChromaDB if available, JSONL fallback."""
    init_palace()
    drawer.metadata = _sanitize_metadata(drawer.metadata)

    if HAS_CHROMA:
        client = chromadb.PersistentClient(path=str(PALACE_DIR / "chroma"))
        collection = client.get_or_create_collection("prediction_drawers")
        collection.add(
            ids=[drawer.drawer_id],
            documents=[drawer.content],
            metadatas=[{
                "wing": drawer.wing,
                "hall": drawer.hall,
                "room": drawer.room,
                "created_at": drawer.created_at,
                **{k: str(v) for k, v in drawer.metadata.items()},
            }],
        )
    else:
        fallback_file = PALACE_DIR / "drawers.jsonl"
        with open(fallback_file, "a") as f:
            f.write(json.dumps(asdict(drawer)) + "\n")

    # Mirror into sqlite-vec semantic index if available. No-op when the
    # deps aren't installed (lib.memory_vec short-circuits). This gives us
    # a ChromaDB-independent semantic search path alongside the existing one.
    try:
        from lib import memory_vec
        memory_vec.index_drawer(
            drawer.drawer_id, drawer.content,
            wing=drawer.wing, hall=drawer.hall, room=drawer.room,
        )
    except Exception:
        pass  # never block writes on vector indexing

    return drawer.drawer_id


def search_memory(
    query: str,
    wing: str | None = None,
    hall: str | None = None,
    room: str | None = None,
    n_results: int = 5,
) -> list[dict]:
    """
    Semantic search across the palace. Filters by wing/hall/room if provided.

    Preference order:
      1. ChromaDB (if installed and has data)
      2. sqlite-vec + sentence-transformers (if deps installed)
      3. JSONL keyword fallback
    """
    if HAS_CHROMA:
        init_palace()
        client = chromadb.PersistentClient(path=str(PALACE_DIR / "chroma"))
        collection = client.get_or_create_collection("prediction_drawers")

        where_filter = {}
        if wing:
            where_filter["wing"] = wing
        if hall:
            where_filter["hall"] = hall
        if room:
            where_filter["room"] = room

        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where_filter if where_filter else None,
        )

        memories = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                dist = results["distances"][0][i] if results["distances"] else 0
                memories.append({
                    "content": doc,
                    "metadata": meta,
                    "relevance": round(1 - dist, 4),
                    "drawer_id": results["ids"][0][i] if results["ids"] else "",
                })
        if memories:
            return memories

    # Second preference: sqlite-vec semantic search (lighter than ChromaDB).
    try:
        from lib import memory_vec
        if memory_vec.semantic_search_available():
            hits = memory_vec.search(query, k=n_results, wing=wing)
            if hits:
                return [{
                    "content": h["text"],
                    "metadata": {"wing": h["wing"], "hall": h["hall"], "room": h["room"]},
                    "relevance": h["similarity"],
                    "drawer_id": h["drawer_id"],
                } for h in hits]
    except Exception:
        pass

    return _search_fallback(query, wing, n_results)


def _search_fallback(query: str, wing: str | None, n: int) -> list[dict]:
    """Keyword search fallback when ChromaDB isn't available."""
    fallback_file = PALACE_DIR / "drawers.jsonl"
    if not fallback_file.exists():
        return []

    results = []
    query_lower = query.lower()
    with open(fallback_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                drawer = json.loads(line)
            except json.JSONDecodeError:
                continue
            if wing and drawer.get("wing") != wing:
                continue
            if query_lower in drawer.get("content", "").lower():
                results.append({
                    "content": drawer["content"],
                    "metadata": drawer.get("metadata", {}),
                    "relevance": 0.5,
                    "drawer_id": drawer.get("drawer_id", ""),
                })

    return results[:n]


def _resolve_wing(category: str) -> str:
    """Map a market category to the appropriate wing."""
    category_lower = category.lower() if category else "other"
    wing_key = f"wing_{category_lower}"
    if wing_key in WINGS:
        return wing_key

    # Fuzzy match by keywords
    for wname, wdata in WINGS.items():
        if any(kw in category_lower for kw in wdata["keywords"]):
            return wname

    return "wing_market"  # Default fallback


# ── Knowledge Graph — Temporal Triples ────────────────────────────

_kg_initialized = False


def _init_kg_db():
    """Initialize the knowledge graph SQLite database. Idempotent."""
    global _kg_initialized
    KG_DB.parent.mkdir(parents=True, exist_ok=True)
    if not _kg_initialized:
        conn = sqlite3.connect(str(KG_DB))
        conn.execute("PRAGMA journal_mode=WAL")  # Concurrent-safe
        conn.execute("""
            CREATE TABLE IF NOT EXISTS triples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                metadata TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subject ON triples(subject)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_predicate ON triples(predicate)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_object ON triples(object)")
        conn.commit()
        conn.close()
        _kg_initialized = True


def kg_add(subject: str, predicate: str, obj: str,
           valid_from: str | None = None, metadata: dict | None = None):
    """
    Add a fact to the knowledge graph.

    Examples:
        kg_add("market_ABC", "bought_yes", "0.65", valid_from="2024-05-01")
        kg_add("politics", "win_rate", "0.72")
        kg_add("forecaster", "brier_score", "0.18")
    """
    _init_kg_db()
    safe_meta = _sanitize_metadata(metadata) if metadata else None
    conn = sqlite3.connect(str(KG_DB))
    conn.execute(
        "INSERT INTO triples (subject, predicate, object, valid_from, metadata) VALUES (?, ?, ?, ?, ?)",
        (subject, predicate, obj,
         valid_from or datetime.now(timezone.utc).isoformat(),
         json.dumps(safe_meta) if safe_meta else None),
    )
    conn.commit()
    conn.close()


def kg_invalidate(subject: str, predicate: str, obj: str, ended: str | None = None):
    """Mark a fact as no longer current (keeps history)."""
    _init_kg_db()
    end_time = ended or datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(KG_DB))
    conn.execute(
        "UPDATE triples SET valid_to = ? WHERE subject = ? AND predicate = ? AND object = ? AND valid_to IS NULL",
        (end_time, subject, predicate, obj),
    )
    conn.commit()
    conn.close()


def kg_query(subject: str, current_only: bool = True, as_of: str | None = None) -> list[dict]:
    """Query all facts about an entity."""
    _init_kg_db()
    conn = sqlite3.connect(str(KG_DB))
    conn.row_factory = sqlite3.Row

    if as_of:
        rows = conn.execute(
            "SELECT * FROM triples WHERE subject = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (subject, as_of, as_of),
        ).fetchall()
    elif current_only:
        rows = conn.execute(
            "SELECT * FROM triples WHERE subject = ? AND valid_to IS NULL",
            (subject,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM triples WHERE subject = ?",
            (subject,),
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def kg_timeline(subject: str) -> list[dict]:
    """Chronological story of an entity — all facts ordered by time."""
    _init_kg_db()
    conn = sqlite3.connect(str(KG_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM triples WHERE subject = ? OR object = ? ORDER BY valid_from ASC",
        (subject, subject),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Agent Diaries ─────────────────────────────────────────────────

def diary_write(agent_name: str, entry: str):
    """
    Write to an agent's diary. Append-only, forensic-grade.

    Examples:
        diary_write("strategy_agent", "PROPOSE|polymarket|ABC|YES|edge_+8%|score_7/9")
        diary_write("risk_agent", "VETO|kalshi|DEF|cat_politics_42%|max_40%")
        diary_write("consensus", "APPROVED|manifold|GHI|YES|edge_+12%|kelly_$7.50")
    """
    DIARY_DIR.mkdir(parents=True, exist_ok=True)

    # Validate agent name — prevent path traversal
    safe_name = "".join(c for c in agent_name if c.isalnum() or c == "_")
    if not safe_name:
        return

    diary_file = DIARY_DIR / f"{safe_name}.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": entry[:1000],  # Cap entry length
    }
    with open(diary_file, "a") as f:
        f.write(json.dumps(record) + "\n")


def diary_read(agent_name: str, last_n: int = 20) -> list[dict]:
    """Read the last N entries from an agent's diary."""
    safe_name = "".join(c for c in agent_name if c.isalnum() or c == "_")
    diary_file = DIARY_DIR / f"{safe_name}.jsonl"
    if not diary_file.exists():
        return []

    entries = []
    with open(diary_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return entries[-last_n:]


# ── Prediction Market Memory Helpers ──────────────────────────────

def remember_trade_decision(
    market_id: str,
    platform: str,
    question: str,
    category: str,
    side: str,
    entry_price: float,
    reasoning: str,
    forecast_sources: dict | None = None,
):
    """
    Store a complete trade decision in the palace.
    Creates both a drawer (verbatim reasoning) and KG triple (structured).
    """
    wing = _resolve_wing(category)

    add_drawer(Drawer(
        wing=wing,
        hall="hall_facts",
        room=f"{category.lower()}-trades",
        content=reasoning,
        metadata={
            "market_id": market_id,
            "platform": platform,
            "question": question[:200],
            "side": side,
            "entry_price": entry_price,
            "sources": json.dumps(forecast_sources) if forecast_sources else "{}",
        },
    ))

    kg_add(market_id, f"bought_{side.lower()}", str(entry_price),
           metadata={"platform": platform, "category": category})


def remember_resolution(
    market_id: str,
    platform: str,
    category: str,
    outcome: str,
    profit: float,
    our_probability: float,
):
    """Store a market resolution and P/L for calibration learning."""
    wing = _resolve_wing(category)

    add_drawer(Drawer(
        wing=wing,
        hall="hall_events",
        room=f"{category.lower()}-resolutions",
        content=f"Market {market_id} resolved {outcome}. P/L: ${profit:+.2f}. "
                f"Our estimate was {our_probability:.0%}.",
        metadata={
            "market_id": market_id,
            "platform": platform,
            "outcome": outcome,
            "profit": profit,
            "our_probability": our_probability,
        },
    ))

    kg_invalidate(market_id, f"bought_yes", "", ended=datetime.now(timezone.utc).isoformat())
    kg_invalidate(market_id, f"bought_no", "", ended=datetime.now(timezone.utc).isoformat())
    kg_add(market_id, "resolved", outcome,
           metadata={"profit": profit, "our_prob": our_probability})


def remember_category_insight(category: str, insight: str):
    """Store a discovered pattern about a market category."""
    wing = _resolve_wing(category)
    add_drawer(Drawer(
        wing=wing,
        hall="hall_discoveries",
        room=f"{category.lower()}-insights",
        content=insight,
        metadata={"category": category},
    ))


def remember_calibration_snapshot(brier: float, log_loss_val: float, source_accuracy: dict):
    """Record a calibration milestone for Hermes to reference."""
    kg_add("forecaster", "brier_score", str(round(brier, 4)))
    kg_add("forecaster", "log_loss", str(round(log_loss_val, 4)))

    add_drawer(Drawer(
        wing="wing_strategy",
        hall="hall_events",
        room="calibration-history",
        content=f"Brier: {brier:.4f} | LogLoss: {log_loss_val:.4f} | "
                f"Sources: {json.dumps(source_accuracy)}",
        metadata={"brier": brier, "log_loss": log_loss_val},
    ))


def recall_category_history(category: str) -> dict:
    """
    Get everything the bot remembers about a category:
    - KG facts (win rates, insights)
    - Recent memories (trade reasoning, resolutions)
    - Agent diary entries mentioning this category
    """
    wing = _resolve_wing(category)
    kg_facts = kg_query(category.lower(), current_only=False)
    memories = search_memory(category, wing=wing, n_results=10)

    agent_mentions = {}
    for agent in ["strategy_agent", "risk_agent", "compliance_agent", "consensus"]:
        entries = diary_read(agent, last_n=50)
        mentions = [e for e in entries if category.lower() in e.get("entry", "").lower()]
        if mentions:
            agent_mentions[agent] = mentions[-5:]

    return {
        "category": category,
        "kg_facts": kg_facts,
        "memories": memories,
        "agent_mentions": agent_mentions,
    }


def get_category_win_rate(category: str) -> float | None:
    """Check if we have a stored win rate for this category."""
    facts = kg_query(category.lower(), current_only=True)
    for f in facts:
        if f["predicate"] == "win_rate":
            try:
                return float(f["object"])
            except ValueError:
                continue
    return None
