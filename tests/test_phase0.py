"""
Phase 0 验证脚本

验证核心假设：
1. DOI → metadata 获取
2. Work identity resolution
3. Abstract → L1 structured output
4. Validator 硬约束
5. SQLite + Markdown 存储

使用真实文献测试，人工检查准确性
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Force UTF-8 output on Windows consoles (avoids GBK encode errors for ✓/✗/⚠)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from src.core.database import Database
from src.core.models import Record, EvidenceLevel, PublicationStatus
from src.core.work_identity import WorkIdentityResolver
from src.core.config import load_config
from src.core.ingestion import get_or_create_record
from src.adapters.crossref import CrossrefAdapter, parse_crossref_metadata
from src.llm.client import create_llm_client
from src.workflows.l1_generator import generate_l1_card
from skills.l1_literature_card.contract import L1Input, L1Output
from skills.l1_literature_card.validator import validate_l1_output


# 测试用的 DOI 列表（覆盖不同场景）
# 注意：部分 DOI 可能无法获取或无 abstract，测试会优雅处理
TEST_DOIS = [
    "10.1038/s41586-021-03819-2",  # AlphaFold2 (Nature 2021) - has abstract
    "10.1126/science.abj6987",     # Science - complete human genome - has abstract
    "10.1073/pnas.2016239118",     # PNAS - protein language models - has abstract
    "10.7554/eLife.64283",         # eLife - ribosome recycling - has abstract
    "10.1126/science.abb2507",      # Science 2020 - SARS-CoV-2 spike cryo-EM (Wrapp et al.)
    "10.1016/j.cell.2019.05.031",  # Cell 2019 - Seurat v3 single-cell integration (Stuart et al.)
    "10.1038/s41586-023-06221-2",  # Nature 2023 - AlphaFold protein-ligand
    "10.1126/science.abq1158",     # Science 2022 - protein design
    "10.1016/j.cell.2020.11.005",  # Cell 2020 - mRNA vaccine
    "10.1371/journal.pcbi.1007589",# PLoS Comp Bio - deep learning
]


async def test_metadata_fetch(crossref: CrossrefAdapter):
    """测试 DOI → metadata 获取"""
    print("\n=== Test 1: Metadata Fetch ===")

    for doi in TEST_DOIS:
        print(f"\nFetching: {doi}")
        try:
            work = await crossref.get_work_by_doi(doi)
            metadata = parse_crossref_metadata(work)

            print(f"  Title: {metadata['title'][:80]}...")
            print(f"  Authors: {len(metadata['authors'])} authors")
            print(f"  Journal: {metadata['journal']}")
            print(f"  Date: {metadata['publication_date']}")
            print(f"  Status: {metadata['publication_status']}")
            print(f"  Has abstract: {metadata['abstract'] is not None}")

        except Exception as e:
            print(f"  ERROR: {e}")


async def test_work_resolution(db: Database, crossref: CrossrefAdapter):
    """测试 Work identity resolution"""
    print("\n=== Test 2: Work Identity Resolution ===")

    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        for doi in TEST_DOIS:
            print(f"\nResolving: {doi}")
            try:
                # 获取元数据
                crossref_work = await crossref.get_work_by_doi(doi)
                metadata = parse_crossref_metadata(crossref_work)
                relations = await crossref.get_relations(doi)
            except Exception as e:
                print(f"  SKIP (metadata fetch failed): {e}")
                continue

            # 使用 get_or_create_record 处理重复 DOI
            def record_factory():
                return Record(
                    record_id=f"R_{doi.replace('/', '_')}",
                    work_id=None,  # 将由 resolver 设置
                    title=metadata["title"],
                    authors=metadata["authors"],
                    journal=metadata["journal"],
                    publication_date=metadata["publication_date"],
                    doi=doi,
                    abstract=metadata["abstract"],
                    evidence_level=(
                        EvidenceLevel.E1.value if metadata["abstract"] else EvidenceLevel.E0.value
                    ),
                    publication_status=metadata["publication_status"],
                    extra_metadata=metadata["other_ids"],
                )

            record, created = await get_or_create_record(session, doi, record_factory)

            if not created:
                print(f"  Record already exists: {record.record_id}")
                continue

            # 解析 Work
            work = await resolver.resolve_or_create_work(record, relations)
            record.work_id = work.id

            print(f"  Work ID: {work.work_id}")
            print(f"  Canonical DOI: {work.canonical_doi}")
            print(f"  Evidence: Check identity_edges table")

        await session.commit()

        # 检查是否有需要人工审核的 edges
        candidates = await resolver.get_candidate_edges()
        if candidates:
            print(f"\n  ⚠ {len(candidates)} edges need manual review")
        else:
            print("\n  ✓ All identity edges auto-resolved")


async def test_l1_generation(db: Database, llm_client):
    """测试 Abstract → L1 generation"""
    print("\n=== Test 3: L1 Generation ===")

    async with db.get_session() as session:
        # 获取有 abstract 的 records
        from sqlalchemy import select

        stmt = select(Record).where(Record.evidence_level == EvidenceLevel.E1.value)
        result = await session.execute(stmt)
        records = result.scalars().all()

        for record in records:
            print(f"\nProcessing: {record.doi}")

            # 准备输入
            input_data = L1Input(
                work_id=f"W{record.work_id}",
                record_id=record.record_id,
                title=record.title,
                authors=[a["name"] for a in record.authors],
                abstract=record.abstract,
                journal=record.journal,
                publication_date=(
                    record.publication_date.isoformat() if record.publication_date else None
                ),
                evidence_level=record.evidence_level,
            )

            # 调用真实 LLM
            try:
                output = await generate_l1_card(input_data, llm_client)
                print(f"  ✓ L1 generation successful")
                print(f"  One-sentence: {output.one_sentence}")
                print(f"  Tags: {output.tags}")
                print(f"  Research object: {output.research_object}")
                print(f"  Major method: {output.major_method}")
            except Exception as e:
                print(f"  ✗ L1 generation failed: {e}")


async def test_markdown_export(db: Database):
    """测试 Markdown 导出"""
    print("\n=== Test 4: Markdown Export ===")

    output_dir = Path("archives/test")
    output_dir.mkdir(parents=True, exist_ok=True)

    async with db.get_session() as session:
        from sqlalchemy import select

        stmt = select(Record).limit(3)
        result = await session.execute(stmt)
        records = result.scalars().all()

        for record in records:
            # 生成 L0 metadata card
            markdown = f"""# {record.title}

## Metadata

- **DOI**: {record.doi}
- **Journal**: {record.journal}
- **Date**: {record.publication_date}
- **Authors**: {', '.join(a['name'] for a in record.authors[:3])}{'...' if len(record.authors) > 3 else ''}

## Evidence

- **Level**: {record.evidence_level}
- **Status**: {record.publication_status}

## Abstract

{record.abstract if record.abstract else '*Not available*'}

---
*Generated: {datetime.utcnow().isoformat()}*
"""

            # 保存
            filename = f"{record.record_id}.md"
            filepath = output_dir / filename
            filepath.write_text(markdown, encoding="utf-8")
            print(f"  Saved: {filepath}")


async def main():
    """Phase 0 验证主流程"""
    print("=" * 60)
    print("Phase 0 Validation")
    print("=" * 60)

    # 加载配置
    config = load_config()

    # 初始化
    db_path = Path(config.database.path).parent / "test_phase0.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = Database(db_path)
    await db.init_db()

    crossref = CrossrefAdapter(email=config.crossref.email)
    llm_client = await create_llm_client(config.llm.model_dump(), model_tier="cheap")

    try:
        # 运行测试
        await test_metadata_fetch(crossref)
        await test_work_resolution(db, crossref)
        await test_l1_generation(db, llm_client)
        await test_markdown_export(db)

        print("\n" + "=" * 60)
        print("Phase 0 Validation Complete")
        print("=" * 60)
        print("\nNext steps:")
        print("1. 人工检查 Work identity accuracy")
        print("2. 人工检查 L1 factual accuracy")
        print("3. 检查 data/test_phase0.db 的数据质量")
        print("4. 检查 archives/test/ 的 Markdown 输出")

    finally:
        await crossref.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
