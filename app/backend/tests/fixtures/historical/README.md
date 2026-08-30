# Reduced public fixture provenance

These are deterministic row-reduced derivatives of public AEMO NEMWeb
artifacts fetched on 2026-08-30. No private or credential-bearing data is
present.

- `dispatch-scada-20260830-1455-reduced.csv` derives from
  `PUBLIC_DISPATCHSCADA_202608301455_0000000535210764.zip`, source SHA-256
  `63192068b79e7eab0a0f5aa87d3d22bca53c62786e8df1672e50d2c48500b126`.
  It retains the authentic envelope and rows for ADPBA1, BBATTERY1 and HPR1.
- `dispatch-price-20260830-1500-reduced.csv` derives from
  `PUBLIC_DISPATCHIS_202608301500_0000000535211318.zip`, source SHA-256
  `fb22ab052a31020fcfcc574b5c2d29c43a2ee4caba903a634ce0980091ac7381`.
  It retains the authentic PRICE v5 envelope and all five regions, including
  negative prices.

The reduced fixture trailer row count is recomputed for the retained records.
