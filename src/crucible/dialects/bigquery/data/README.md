# Vendored data

## public_suffix_list.dat

The [Public Suffix List](https://publicsuffix.org/), used by `NET.REG_DOMAIN`
(`../net.py`). Unmodified; licensed under the Mozilla Public License 2.0 (see its header and
`NOTICE`). Crucible reads only its ICANN section, as BigQuery does.

- Source: https://github.com/publicsuffix/list, `public_suffix_list.dat`
- Commit: `8437e5262f13305f94087abe699b4fe2be1d1de8` (2026-10-22)
- SHA-256: `d3e6243b51695327984abb3e2a5aa7077096867f75121e031e6a5a3840ca8bcd`

Crucible never fetches the list at run time. BigQuery uses its own snapshot, so a suffix added or
removed after one of the two snapshots can give a different answer. To update, replace the file
with the one at a newer commit, update the commit and checksum here, and run the `net` corpus
cases.
