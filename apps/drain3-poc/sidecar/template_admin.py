"""Local POC-only template review/publish/rollback CLI; not an authenticated API."""
import argparse
from contextlib import closing
import psycopg2
import app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review")
    review.add_argument("candidate_id", type=int)
    review.add_argument("decision", choices=("approve", "reject"))
    publish = sub.add_parser("publish")
    publish.add_argument("candidate_id", type=int)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("scope", help="canonical JSON scope from poc_v2_templates")
    rollback.add_argument("version", type=int)
    for cmd in (review, publish, rollback):
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--note", required=True)
    args = parser.parse_args()
    with closing(psycopg2.connect(app.PG_DSN)) as conn:
        if args.command == "review":
            result = app.review_candidate(conn, args.candidate_id, args.decision, args.actor, args.note)
        elif args.command == "publish":
            result = app.publish_candidate(conn, args.candidate_id, args.actor, args.note)
        else:
            result = app.rollback_version(conn, args.scope, args.version, args.actor, args.note)
    print(result)


if __name__ == "__main__":
    main()
