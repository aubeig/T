# Защита _cleanup_business_messages + проверка целей в _send_job_to_salute
# Добавь в anon.py перед вызовами deleteMessages / send в мост
PATCH_NOTES = """
1) _cleanup_business_messages — не удалять если !enabled; только business_connection_id
2) _send_job_to_salute — не включать chat_id получателя в targets
3) _complete_asr_job — проверять recipient == job['recipient'] перед send
"""
