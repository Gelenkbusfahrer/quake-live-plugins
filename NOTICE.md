# Attribution and licenses

## New contributions

Copyright (c) 2026, Gelenkbusfahrer.

`src/plugins/balance_guard.py`, `openai_bot_role_balance_guard.txt`, the tests,
new examples and documentation are distributed under BSD-3-Clause; see
[`LICENSE`](LICENSE). The additions to Bob implement bounded history reading,
incremental token accounting and the optional guard-comment bridge.

## Bob and auto_rebalance

`src/plugins/openai_bot.py` is a modified server variant of ShiN0's
`src/experimental/openai_bot.py` from
[mgaertne/minqlx-plugin-tests](https://github.com/mgaertne/minqlx-plugin-tests).
Its original source header (Copyright (c) 2023 ShiN0) is retained.
`compat/auto_rebalance.py` retains its original Copyright (c) 2018 ShiN0 header.
Neither file is presented as a new official upstream release.

The upstream repository's BSD-3-Clause license at reference commit
`7bef117a42a7c238934d479dce57484e1c0991c6` is preserved verbatim in
[`licenses/BSD-3-Clause-upstream.txt`](licenses/BSD-3-Clause-upstream.txt),
including Copyright (c) 2017, Markus Gärtner and the no-endorsement condition.
The reference commit identifies the checked upstream/license, not an assertion
that the included modified server files are byte-identical to upstream.

## Reference balance plugin

`compat/balance.py` is a pre-existing modified minqlx balance variant. Its
original Copyright (C) 2015 Mino header and **GPL version 3 or later** notice
are retained. The full GPL version 3 text is included in
[`licenses/GPL-3.0.txt`](licenses/GPL-3.0.txt). The root BSD license does not
relicense this file. A complete history of earlier changes to this variant
has not been reconstructed; it is provided as the matching interface reference.

## Source handling

The two runtime plugins, event prompt, three test files and two reference
balancers were copied byte-for-byte from the reviewed working sources for this
publication. No production configurations, database files, player records,
server logs, original personality files or internal operations reports are
included. This repository begins with a separate history rather than copying
the private administration repository's history. Neutral prompt examples are
new files and are explicitly not copies of production prompts.
