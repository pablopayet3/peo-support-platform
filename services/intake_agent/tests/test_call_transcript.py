from intake_agent.agent import _conversation

NOTE = ("Call transcript:\n\n00:01CustomerHi. This is Luis Ortega. What's my deductible this year? Thanks.\n\n"
        "If you redact any data from this comment, you must also delete the call recording file.")


def test_zendesk_call_transcript_note_is_read_as_the_callers_words():
    comments = [{"public": False, "type": "VoiceComment", "data": {}},
                {"public": False, "plain_body": NOTE, "author_id": 1}]
    raw, _ = _conversation(comments, requester_id=5)
    assert raw == "Customer: Hi. This is Luis Ortega. What's my deductible this year? Thanks."


def test_other_internal_notes_stay_out_of_the_conversation():
    comments = [{"public": True, "plain_body": "Hello", "author_id": 5},
                {"public": False, "plain_body": "AI support agent summary", "author_id": 1}]
    assert _conversation(comments, requester_id=5)[0] == "Customer: Hello"
