-- Run as the local CNPG postgres administrator AFTER migration 0003.
-- CNPG owns LOGIN/passwords; migration 0003 owns SELECT grants and future-table grants.
-- No credentials belong in SQL, Terraform state, or logs.
GRANT skillpulse_readonly TO skillpulse_api, skillpulse_mcp;
ALTER ROLE skillpulse_api SET default_transaction_read_only = on;
ALTER ROLE skillpulse_mcp SET default_transaction_read_only = on;
ALTER ROLE skillpulse_api SET statement_timeout = '5s';
ALTER ROLE skillpulse_mcp SET statement_timeout = '5s';
