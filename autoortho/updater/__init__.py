"""AutoOrtho self-updater.

The pieces, in the order an update runs:

* :mod:`.install_info` works out how this copy was installed and whether it
  can replace itself.
* :mod:`.releases` and :mod:`.manifest` find the newest release on GitHub and
  the file that matches this install.
* :mod:`.download` fetches it and checks its size and SHA-256.
* :mod:`.stage` unpacks it into a staging folder and checks the result.
* :mod:`.plan` describes the swap; :mod:`.launch` starts the helper.
* :mod:`.helper` runs in a separate process (``AO_RUN_MODE=apply_update``)
  once AutoOrtho has quit: it swaps the folders, relaunches, and rolls back
  when the new version fails to start.

Nothing here imports Qt, so the helper starts quickly and the logic is easy
to test.
"""
