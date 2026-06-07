"""WSGI entrypoint for HBExtra."""

import threading

import hbextra

hbextra.init_db()
threading.Thread(target=hbextra.refresh_scheduler, daemon=True).start()
threading.Thread(target=hbextra.tag_loader_bg, daemon=True).start()

application = hbextra.app
