"""Human-side commands: set up the database and make the decisions agents are not allowed to make.

    python -m ontology.cli init
    python -m ontology.cli changesets [--status pending]
    python -m ontology.cli show <changeset_id>
    python -m ontology.cli accept <changeset_id> | reject <changeset_id> | revert [<changeset_id>]
    python -m ontology.cli types
    python -m ontology.cli accept-type <object|link> <name> | reject-type <object|link> <name>
    python -m ontology.cli bindings [<node_id>]
    python -m ontology.cli collect                     # one collection pass (templates + due series)
    python -m ontology.cli import-csv <node_id> <file.csv> --name "ETF 净流入" [--role value] [--unit USD] [--frequency 1d]
    python -m ontology.cli projects                    # every ontology project
    python -m ontology.cli project-export <id> [file]  # portable JSON; project-import <file> [--name ...] to load one

Pass --project <id> for commands that operate on an existing project.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime

from .db import connect, migrate
from .providers import load_env_file
from .errors import OntologyError
from . import projects
from .sources import Obs
from .store import OntologyStore


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ontology")
    p.add_argument("--project", help="project id for project-scoped commands")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("projects")
    ex = sub.add_parser("project-export")
    ex.add_argument("project_id")
    ex.add_argument("file", nargs="?")
    im = sub.add_parser("project-import")
    im.add_argument("file")
    im.add_argument("--name")
    claim = sub.add_parser("project-claim", help="assign one reviewed legacy project to a CloudBase user ID")
    claim.add_argument("project_id")
    claim.add_argument("user_id")
    sub.add_parser("migrate", help="apply pending migrations to the registry and every project")
    sub.add_parser("init", help="apply migrations and load the seed types")
    ls = sub.add_parser("changesets")
    ls.add_argument("--status")
    for name in ("show", "accept", "reject"):
        sub.add_parser(name).add_argument("changeset_id")
    sub.add_parser("revert").add_argument("changeset_id", nargs="?")
    sub.add_parser("types")
    for name in ("accept-type", "reject-type"):
        sp = sub.add_parser(name)
        sp.add_argument("kind", choices=["object", "link"])
        sp.add_argument("name")
    sub.add_parser("bindings").add_argument("node_id", nargs="?")
    sub.add_parser("collect")
    imp = sub.add_parser("import-csv", help="load date,value rows into a manual series bound to a node")
    imp.add_argument("node_id")
    imp.add_argument("file")
    imp.add_argument("--name", required=True)
    imp.add_argument("--role", default="value")
    imp.add_argument("--unit")
    imp.add_argument("--frequency", default="1d")
    args = p.parse_args(argv)

    load_env_file()
    reg = connect()
    projects.ensure_registry(reg)
    try:
        if args.cmd == "migrate":
            applied = projects.upgrade_all(reg)
            for schema, versions in applied.items():
                print(f"{schema}: {', '.join(versions)}")
            print("已是最新。" if not applied else f"已升级 {len(applied)} 个 schema。")
            return 0
        if args.cmd == "init" and args.project is None:
            print("项目目录已初始化。请在工作台创建研究项目。")
            return 0
        if args.cmd == "projects":
            _print([{k: p[k] for k in ("id", "name", "status", "stats", "last_activity")} for p in projects.list_all(reg)])
            return 0
        if args.cmd == "project-export":
            doc = projects.export(reg, args.project_id)
            text = json.dumps(doc, ensure_ascii=False, indent=2, default=str)
            if args.file:
                with open(args.file, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"{len(doc['nodes'])} nodes, {len(doc['links'])} links written to {args.file}")
            else:
                print(text)
            return 0
        if args.cmd == "project-import":
            with open(args.file, encoding="utf-8") as f:
                p = projects.import_doc(reg, json.load(f), args.name)
            print(f"imported as project {p['id']} ({p['name']}): {p['stats']['nodes']} nodes, {p['stats']['links']} links")
            return 0
        if args.cmd == "project-claim":
            projects.claim_legacy(reg, args.project_id, args.user_id)
            print(f"已绑定项目 {args.project_id}。")
            return 0
        if args.project is None:
            p.error("--project is required for this command")
        conn = projects.project_connect(args.project)
    except OntologyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.cmd == "init":
        migrate(conn)
    store = OntologyStore(conn, actor="human")
    try:
        if args.cmd == "init":
            store.sync_seed_types()
            print(f"{len(store.registry.object_types)} object types, {len(store.registry.link_types)} link types")
        elif args.cmd == "changesets":
            _print(store.list_changesets(args.status))
        elif args.cmd == "show":
            _print(store.get_changeset(args.changeset_id))
        elif args.cmd == "accept":
            _print(store.accept_changeset(args.changeset_id))
        elif args.cmd == "reject":
            _print(store.reject_changeset(args.changeset_id))
        elif args.cmd == "revert":
            _print(store.revert_changeset(args.changeset_id))
        elif args.cmd == "types":
            _print(store.get_schema())
        elif args.cmd in ("accept-type", "reject-type"):
            store.decide_type(args.kind, args.name, accept=args.cmd == "accept-type")
            print(f"{args.kind} type {args.name}: {'accepted' if args.cmd == 'accept-type' else 'rejected'}")
        elif args.cmd == "bindings":
            _print(store.list_bindings(args.node_id))
        elif args.cmd == "collect":
            from .collector import run_once

            print(f"{len(run_once(conn))} series updated")
        elif args.cmd == "import-csv":
            points = _read_csv(args.file)
            b = store.bind_direct(args.node_id, "manual", {"name": args.name, "frequency": args.frequency}, args.role,
                                  f"手动导入 {args.file}", {"kind": "user", "note": args.file}, unit=args.unit)["binding"]
            print(f"{store.series.write(b['series_id'], points)} points written to {b['label']} ({b['series_id']})")
    except OntologyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _read_csv(path: str) -> list[Obs]:
    """First column a date or datetime, second a number; a header row and blank values are skipped."""
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2 or not row[1].strip():
                continue
            try:
                ts = datetime.fromisoformat(row[0].strip())
                value = float(row[1].replace(",", ""))
            except ValueError:
                continue  # header or a note line
            out.append(Obs(ts if ts.tzinfo else ts.replace(tzinfo=UTC), value))
    if not out:
        raise OntologyError(f"no date,value rows found in {path}")
    return out


if __name__ == "__main__":
    sys.exit(main())
