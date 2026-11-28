SELECT count(*)
FROM conversation_threads ct
WHERE ct.current_status = 'in_progress'
  AND NOT (
    ct.updated_at < NOW() - INTERVAL '15 minutes'
    AND EXISTS (
      SELECT 1
      FROM automation_executions ae
      WHERE ae.conversation_thread_id = ct.conversation_thread_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM automation_executions ae
      WHERE ae.conversation_thread_id = ct.conversation_thread_id
        AND ae.status NOT IN ('completed', 'failed', 'skipped', 'cancelled', 'timeout')
    )
    AND NOT EXISTS (
      SELECT 1
      FROM conversation_responses cr
      WHERE cr.conversation_thread_id = ct.conversation_thread_id
        AND cr.status = 'in_progress'
    )
  )
