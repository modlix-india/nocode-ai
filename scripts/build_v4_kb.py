"""One-shot builder for the v4 platform KB.

Walks three source trees:
  - nocode-ai/app/agents/appbuilder/aicontext/   (v3-era curated agent docs)
  - nocode-saas/docs/contribution/               (platform team docs)
  - modlix-mcp/skills/                           (skill recipes)

Categorises every file into one of:
  kb/security/...        kb/ui/...           kb/core/...
  kb/entity-processor/...  kb/shared/...     kb/workflows/...

Copies each source file into its categorised slot under
`app/agents/appbuilderv4/kb/`. Idempotent — re-running overwrites with
the latest source content.

Run with: venv/bin/python scripts/build_v4_kb.py
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
KB_ROOT = REPO / "app" / "agents" / "appbuilderv4" / "kb"

AICONTEXT = REPO / "app" / "agents" / "appbuilder" / "aicontext"
SAAS_DOCS = REPO.parent / "nocode-saas" / "docs" / "contribution"
MCP_SKILLS = REPO.parent / "modlix-mcp" / "skills"
# kb_seed/ is v4's own curated tree — written by hand, NOT derived from
# any external source. Mirrors kb/ structure; every file under
# kb_seed/<service>/ gets copied to kb/<service>/ verbatim. This is
# where the lessons we've paid for at runtime get persisted.
KB_SEED = REPO / "app" / "agents" / "appbuilderv4" / "kb_seed"

SERVICES = ("security", "ui", "core", "entity-processor", "shared", "workflows")


# ── Categorisation rules ────────────────────────────────────────────────


def _slug(name: str) -> str:
    """File-system safe lower-kebab slug for KB filenames."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# Map aicontext top-level files → (service, target_basename).
AICONTEXT_TOP_LEVEL: dict[str, tuple[str, str]] = {
    "01-critical-rules.md": ("shared", "critical-rules.md"),
    "02-structure.md": ("ui", "page-structure.md"),
    "03-components.md": ("ui", "components.md"),
    "04-styles-and-themes.md": ("ui", "styles-and-themes.md"),
    "05-events-and-functions.md": ("ui", "events-and-functions.md"),
    "06-patterns.md": ("workflows", "_patterns-index.md"),
}

# Map aicontext/reference/*.md → service.
AICONTEXT_REFERENCE: dict[str, str] = {
    "auth_lifecycle.md": "security",
    "sso3_architecture.md": "security",
    "preauthorize_location.md": "security",
    "design_system.md": "ui",
    "component_layout.md": "ui",
    "component_definition_invariants.md": "ui",
    "multi_valued_properties.md": "ui",
    "versioning_model.md": "ui",
    "preview_urls.md": "shared",
    "link_paths.md": "shared",
    "internal_api_calls.md": "shared",
    "file_uploads.md": "shared",
    "precision.md": "shared",
    "store_pre_mount.md": "ui",
    "branch_awareness.md": "shared",
    "personalization.md": "core",
    "platform_services.md": "shared",
    "kirun_remote_repository.md": "core",
    "editortemplates_app.md": "ui",
    "modlix_detection.md": "ui",
    "dev_mongo.md": "shared",
    "storage_db_readonly.md": "core",
}

# Map nocode-saas/docs/contribution/ → (service, target_basename).
# Module docs → matching service; cross-cutting → shared.
SAAS_DOCS_MAP: dict[str, tuple[str, str]] = {
    "architecture-overview.md":            ("shared", "saas-architecture-overview.md"),
    "security-and-multitenancy.md":        ("security", "multitenancy-model.md"),
    "event-and-messaging.md":              ("entity-processor", "event-and-messaging.md"),
    "analytics-posthog.md":                ("shared", "analytics-posthog.md"),
    "git-workflow.md":                     ("shared", "git-workflow.md"),
    "deployment-and-ci.md":                ("shared", "deployment-and-ci.md"),
    "reactive-programming-guide.md":       ("shared", "reactive-programming.md"),
    "development-setup.md":                ("shared", "development-setup.md"),
    "database-and-jooq.md":                ("shared", "database-and-jooq.md"),
    "coding-conventions.md":               ("shared", "coding-conventions.md"),
    "testing-guide.md":                    ("shared", "testing-guide.md"),
    "README.md":                           ("shared", "contribution-readme.md"),
    "modules/README.md":                   ("shared", "modules-overview.md"),
    "modules/commons-core.md":             ("shared", "module-commons-core.md"),
    "modules/commons.md":                  ("shared", "module-commons.md"),
    "modules/commons2-mq.md":              ("shared", "module-commons2-mq.md"),
    "modules/commons-mq.md":               ("shared", "module-commons-mq.md"),
    "modules/commons-security.md":         ("security", "module-commons-security.md"),
    "modules/commons2-security.md":        ("security", "module-commons2-security.md"),
    "modules/commons-jooq.md":             ("shared", "module-commons-jooq.md"),
    "modules/commons2-jooq.md":            ("shared", "module-commons2-jooq.md"),
    "modules/commons-mongo.md":            ("shared", "module-commons-mongo.md"),
    "modules/commons2.md":                 ("shared", "module-commons2.md"),
    "modules/eureka.md":                   ("shared", "module-eureka.md"),
    "modules/config.md":                   ("shared", "module-config.md"),
    "modules/gateway.md":                  ("shared", "module-gateway.md"),
    "modules/multi.md":                    ("shared", "module-multi.md"),
    "modules/notification.md":             ("shared", "module-notification.md"),
    "modules/message.md":                  ("shared", "module-message.md"),
    "modules/files.md":                    ("shared", "module-files.md"),
    "modules/security.md":                 ("security", "module-security.md"),
    "modules/ui.md":                       ("ui", "module-ui.md"),
    "modules/core.md":                     ("core", "module-core.md"),
    "modules/entity-processor.md":         ("entity-processor", "module-entity-processor.md"),
    "modules/entity-collector.md":         ("entity-processor", "module-entity-collector.md"),
}


def main() -> int:
    # 1. Wipe and recreate kb/ tree.
    if KB_ROOT.exists():
        shutil.rmtree(KB_ROOT)
    for svc in SERVICES:
        (KB_ROOT / svc).mkdir(parents=True, exist_ok=True)

    summary: dict[str, int] = {s: 0 for s in SERVICES}
    skipped: list[str] = []

    # 2. aicontext top-level chapters
    for fname, (service, target) in AICONTEXT_TOP_LEVEL.items():
        src = AICONTEXT / fname
        if src.exists():
            shutil.copy2(src, KB_ROOT / service / target)
            summary[service] += 1
        else:
            skipped.append(str(src))

    # 3. aicontext/reference/*
    ref_dir = AICONTEXT / "reference"
    for entry in sorted(ref_dir.glob("*.md")) if ref_dir.exists() else []:
        target_service = AICONTEXT_REFERENCE.get(entry.name, "shared")
        target = entry.name.replace("_", "-")
        shutil.copy2(entry, KB_ROOT / target_service / target)
        summary[target_service] += 1

    # 4. aicontext/patterns/*/ → kb/workflows/<pattern>.md (use README.md
    #    as the workflow body; auxiliary files like *.json, *.dsl skipped
    #    here — they're examples the agent can fetch separately if we
    #    later expose them).
    patterns_dir = AICONTEXT / "patterns"
    if patterns_dir.exists():
        for pattern_dir in sorted(patterns_dir.iterdir()):
            if not pattern_dir.is_dir():
                continue
            readme = pattern_dir / "README.md"
            if not readme.exists():
                continue
            target = KB_ROOT / "workflows" / f"{pattern_dir.name}.md"
            shutil.copy2(readme, target)
            summary["workflows"] += 1

    # 5. modlix-mcp/skills/*.md
    if MCP_SKILLS.exists():
        for entry in sorted(MCP_SKILLS.glob("*.md")):
            # All MCP skills are workflow-shaped.
            target = KB_ROOT / "workflows" / f"skill-{entry.name}"
            shutil.copy2(entry, target)
            summary["workflows"] += 1

    # 6. nocode-saas/docs/contribution/
    if SAAS_DOCS.exists():
        for rel, (service, target) in SAAS_DOCS_MAP.items():
            src = SAAS_DOCS / rel
            if src.exists():
                shutil.copy2(src, KB_ROOT / service / target)
                summary[service] += 1
            else:
                skipped.append(str(src))

    # 7. Overlay kb_seed/ — v4's own hand-authored entries (gotchas,
    #    workflow templates, decisions captured at runtime). These COPY
    #    LAST so they win against any same-named file from earlier
    #    sources, and are preserved across re-runs of the build script.
    if KB_SEED.exists():
        for svc in SERVICES:
            seed_dir = KB_SEED / svc
            if not seed_dir.exists():
                continue
            for entry in sorted(seed_dir.glob("*.md")):
                shutil.copy2(entry, KB_ROOT / svc / entry.name)
                summary[svc] += 1

    # 8. Write per-service README.md describing the service
    for svc in SERVICES:
        if svc == "workflows":
            text = (
                f"# Workflows\n\n"
                "Multi-step recipes that may span more than one Modlix service. "
                "Each file describes ONE task with preconditions, ordered call "
                "sequence, expected responses, and failure modes.\n\n"
                "When you need to perform a cross-service task (e.g. create an "
                "app, sign a user up, replace a page), look for a matching "
                "workflow here FIRST before composing your own calls.\n"
            )
        else:
            text = (
                f"# {svc.title()} service KB\n\n"
                "Curated platform knowledge owned by the team. Refreshed on "
                "every deploy from source files in nocode-ai (aicontext), "
                "nocode-saas/docs, and modlix-mcp/skills.\n\n"
                "Files in this directory are READ-ONLY for the agent — to "
                "update, edit the source and re-run scripts/build_v4_kb.py.\n"
            )
        (KB_ROOT / svc / "README.md").write_text(text)

    # 8. Print summary
    print(f"\n=== v4 KB build summary  →  {KB_ROOT.relative_to(REPO)} ===")
    for svc in SERVICES:
        n = len(list((KB_ROOT / svc).glob("*.md")))
        print(f"  {svc:<18s} {n:>4d}  .md files")
    print(f"  TOTAL              {sum(len(list((KB_ROOT / s).glob('*.md'))) for s in SERVICES):>4d}  .md files")
    if skipped:
        print("\n  skipped (source missing):")
        for s in skipped:
            print(f"    {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
