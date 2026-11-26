# dalton-macro

Open-source (`Apache-2.0`) stdio MCP plugin providing official US macroeconomic data from FRED/ALFRED (including historical point-in-time vintage queries) and US Treasury Fiscal Data (Treasury General Account closing balance and Debt to the Penny).

## Tools

- `get_fred_series(series, start, end, vintage=None, limit=None)` — Query FRED or ALFRED CSV endpoints directly without requiring an API key.
- `get_treasury_data(dataset, start, end, limit=None)` — Query official US Treasury Fiscal Data for `dataset="tga"` or `dataset="debt"`.
- `list_macro_catalog()` — List common FRED series IDs and descriptions.
