import hashlib
import json
import uuid
from sqlalchemy import create_engine, select, update
from src.backend_v2.storage.schema import analysis_artifacts, books, analysis_runs
from src.backend_v2.timestamps import utcnow
from src.backend_v2.serialization import canonical_json as _json

def main():
    target_book_id = "3a8c6ca7-347b-49b2-b18b-689dadbd4ba0"
    source_db = "sqlite:///../test_kurumi_ch1_to_ch5.db"
    target_db = "sqlite:///data-v2/saber.sqlite3"

    src_engine = create_engine(source_db)
    with src_engine.connect() as conn:
        row = conn.execute(
            select(analysis_artifacts)
            .where(
                analysis_artifacts.c.kind == "entity_alignment",
                analysis_artifacts.c.is_active == True,
            )
        ).mappings().one()
        payload = json.loads(row["payload_json"])

    print(f"Loaded source payload: {len(payload.get('canonical_entities', []))} canonical entities.")

    # Chapter ID mapping from Venera to saber.sqlite3
    ch_map = {
        "d59724f2-d432-11eb-84f6-00163e0ca5bd": "37949c19-fe6e-429b-a22d-6fa79f826e40", # 第01话
        "9573a200-ebb7-11eb-b340-00163e0ca5bd": "ed9acfac-1c64-4d3b-9e9d-8467493e1f02", # 第02话
        "642c9f7a-023d-11ec-b27f-00163e0ca5bd": "d48ec606-fda8-4f38-9606-57779c0d6d8e", # 第03话
        "18f12752-0d3b-11ec-bb3f-00163e0ca5bd": "3f305bed-3bb7-43bd-99ed-740a06d76289", # 第04话
        "71f7e1ce-228a-11ec-b330-00163e0ca5bd": "03113354-ddf0-4597-b8b9-b226c62a052d", # 第05话
    }

    canonical_entities = payload.get("canonical_entities", [])
    for ent in canonical_entities:
        old_vchs = ent.get("valid_chapters", [])
        new_vchs = [ch_map.get(cid, cid) for cid in old_vchs]
        ent["valid_chapters"] = sorted(list(set(new_vchs)))

    payload_str = _json(payload)
    now = utcnow()
    new_artifact_id = str(uuid.uuid4())
    dep_fingerprint = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    tgt_engine = create_engine(target_db)
    with tgt_engine.begin() as conn:
        # Verify book exists
        book_row = conn.execute(
            select(books.c.id).where(books.c.id == target_book_id)
        ).scalar()
        if not book_row:
            raise RuntimeError(f"Book {target_book_id} not found in target database!")

        # Find latest completed or existing run for this book
        run_row = conn.execute(
            select(analysis_runs.c.id)
            .where(analysis_runs.c.book_id == target_book_id)
            .order_by(analysis_runs.c.created_at.desc())
        ).scalars().first()
        print(f"Target run ID: {run_row}")

        # Deactivate any existing entity_alignment for this book
        conn.execute(
            update(analysis_artifacts)
            .where(
                analysis_artifacts.c.book_id == target_book_id,
                analysis_artifacts.c.kind == "entity_alignment",
                analysis_artifacts.c.template == "default",
                analysis_artifacts.c.is_active == True,
            )
            .values(is_active=False, updated_at=now)
        )

        # Insert new active artifact
        conn.execute(
            analysis_artifacts.insert().values(
                id=new_artifact_id,
                book_id=target_book_id,
                run_id=run_row,
                kind="entity_alignment",
                template="default",
                status="ready",
                revision=1,
                is_active=True,
                dependency_fingerprint=dep_fingerprint,
                payload_json=payload_str,
                asset_id=None,
                created_at=now,
                updated_at=now,
            )
        )

    print(f"Successfully inserted active entity_alignment artifact {new_artifact_id} for book {target_book_id}.")

if __name__ == "__main__":
    main()
