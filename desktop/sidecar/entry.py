"""Entry point of the bundled local workbench (the desktop app's sidecar, frozen by build.sh).

The app starts it with WORKBENCH_HOST/PORT, ONTO_LOCAL_DB=1 and WORKBENCH_SNAPSHOTS=1 and no DATABASE_URL, so the
server runs on its own embedded Postgres (ontology/localdb.py). Started by hand it behaves the same way.
"""

import multiprocessing
import os

if __name__ == "__main__":
    multiprocessing.freeze_support()
    os.environ.setdefault("ONTO_LOCAL_DB", "1")
    os.environ.setdefault("WORKBENCH_SNAPSHOTS", "1")
    os.environ.setdefault("WORKBENCH_COLLECT", "0")  # no background fetching from data sources until asked

    from ontology import db

    db.database_url()  # start the database before the server, so a failure is reported here and plainly

    from workbench.server import main

    main()
