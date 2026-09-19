# Ad-hoc scripts

One-off diagnostic and seeding scripts. These are **not** tests and are not run
by `manage.py test` — several of them execute database queries at import time,
which is why they must live outside the project root where Django's `test*.py`
discovery would pick them up.

Run from the project root:

```bash
python scripts/<name>.py
```

Several of these hardcode primary keys or usernames from one developer's
database (for example `Tournament.objects.get(pk=12)`, or the user `t2p1`) and
will not work unmodified elsewhere. Read a script before running it.
