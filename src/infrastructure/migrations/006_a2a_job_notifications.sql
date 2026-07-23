CREATE OR REPLACE FUNCTION notify_a2a_job()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify('tesseraflow_a2a_jobs', NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS a2a_job_notify_trigger ON a2a_jobs;

CREATE TRIGGER a2a_job_notify_trigger
AFTER INSERT OR UPDATE OF status ON a2a_jobs
FOR EACH ROW
WHEN (NEW.status IN ('queued', 'completed', 'failed', 'cancelled'))
EXECUTE FUNCTION notify_a2a_job();
