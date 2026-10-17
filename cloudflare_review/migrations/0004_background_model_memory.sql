-- Retain proposals and audit history; stop creating a separate human rule queue.
UPDATE model_learning_rules SET status='advisory'
WHERE status IN ('pending_review','approved_guidance');
