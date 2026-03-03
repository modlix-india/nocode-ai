-- Add PROCESSING status to session status enum
ALTER TABLE ai_tracking_sessions
    MODIFY COLUMN STATUS ENUM('ACTIVE', 'PROCESSING', 'COMPLETED', 'EXPIRED') DEFAULT 'ACTIVE';
