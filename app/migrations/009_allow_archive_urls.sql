-- Permit immutable interval receipts extracted from a daily NEMWeb archive.
-- Archive receipt identity is: daily-archive.zip#nested-interval.zip.

BEGIN;

ALTER TABLE dispatch_scada_artifacts
    DROP CONSTRAINT IF EXISTS dispatch_scada_artifacts_check;
ALTER TABLE dispatch_scada_artifacts
    ADD CONSTRAINT dispatch_scada_artifacts_check CHECK (
        source_url =
            'https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/' || zip_filename
        OR source_url =
            'https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/'
            || 'PUBLIC_DISPATCHSCADA_'
            || substring(zip_filename FROM 22 FOR 8)
            || '.zip#' || zip_filename
    );

ALTER TABLE dispatch_price_artifacts
    DROP CONSTRAINT IF EXISTS dispatch_price_artifacts_check;
ALTER TABLE dispatch_price_artifacts
    ADD CONSTRAINT dispatch_price_artifacts_check CHECK (
        source_url =
            'https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/' || zip_filename
        OR source_url =
            'https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/'
            || 'PUBLIC_DISPATCHIS_'
            || substring(zip_filename FROM 19 FOR 8)
            || '.zip#' || zip_filename
    );

COMMIT;
