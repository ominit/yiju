from typing import TYPE_CHECKING

from conftest import add, set_strength, states

from yiju.storage.graph import refresh
from yiju.storage.sentences import set_hidden
from yiju.storage.words import set_manual

if TYPE_CHECKING:
    import sqlite3

SENTENCES = """\
我 很 好 | I am well.
你 很 好 | You are well.
他 吃 饭 喝 茶 | He eats and drinks tea.
"""


def learn(connection: sqlite3.Connection, word: str) -> None:
    set_strength(connection, word)


def see(connection: sqlite3.Connection, word: str) -> None:
    set_strength(connection, word, stability=1, learned=False)


def forget(connection: sqlite3.Connection, word: str) -> None:
    connection.execute("UPDATE words SET learned = 0 WHERE word = ?", (word,))
    refresh(connection, [word])
    connection.commit()


def version(connection: sqlite3.Connection, table: str, key: str) -> int:
    column = "word" if table == "words" else "chinese"
    return int(
        connection.execute(
            f"SELECT version FROM {table} WHERE {column} = ?",  # noqa: S608
            (key,),
        ).fetchone()[0]
    )


def test_a_sentence_is_eligible_when_all_but_two_words_are_learned(
    connection: sqlite3.Connection,
) -> None:
    add(connection, SENTENCES)
    assert set(states(connection, "sentences").values()) == {"locked"}
    assert set(states(connection).values()) == {"locked"}
    learn(connection, "我")
    assert states(connection, "sentences")["我很好"] == "eligible"
    assert states(connection)["很"] == "unlocked"
    assert states(connection)["好"] == "unlocked"
    assert states(connection)["你"] == "locked"


def test_sentences_of_one_or_two_words_are_always_eligible(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "酸苹果 | sour apple\n好 | good\n")
    assert states(connection, "sentences") == {
        "酸苹果": "eligible",
        "好": "eligible",
    }
    assert states(connection) == {
        "酸": "unlocked",
        "苹果": "unlocked",
        "好": "unlocked",
    }


def test_repeated_words_count_once(connection: sqlite3.Connection) -> None:
    add(connection, "好 好 学习 | Study well.\n")
    assert states(connection, "sentences")["好好学习"] == "eligible"


def test_a_sentence_unlocks_once_every_word_is_seen_and_stays_unlocked(
    connection: sqlite3.Connection,
) -> None:
    add(connection, SENTENCES)
    learn(connection, "我")
    see(connection, "很")
    assert states(connection, "sentences")["我很好"] == "eligible"
    see(connection, "好")
    assert states(connection, "sentences")["我很好"] == "unlocked"
    forget(connection, "我")
    assert states(connection, "sentences")["我很好"] == "unlocked"
    assert states(connection)["我"] == "seen"


def test_losing_a_learned_word_relocks_the_sentence_and_its_unseen_words(
    connection: sqlite3.Connection,
) -> None:
    add(connection, SENTENCES)
    learn(connection, "我")
    see(connection, "很")
    forget(connection, "我")
    assert states(connection, "sentences")["我很好"] == "locked"
    assert states(connection)["好"] == "locked"
    assert states(connection)["很"] == "seen"
    assert states(connection)["我"] == "seen"


def test_another_eligible_sentence_keeps_a_shared_word_unlocked(
    connection: sqlite3.Connection,
) -> None:
    add(connection, SENTENCES)
    learn(connection, "我")
    learn(connection, "你")
    forget(connection, "我")
    assert states(connection, "sentences")["我很好"] == "locked"
    assert states(connection, "sentences")["你很好"] == "eligible"
    assert states(connection)["很"] == "unlocked"
    assert states(connection)["好"] == "unlocked"


def test_a_manual_unlock_survives_relocking_until_it_is_undone(
    connection: sqlite3.Connection,
) -> None:
    add(connection, SENTENCES)
    set_manual(connection, "茶", version(connection, "words", "茶"), manual=True)
    assert states(connection)["茶"] == "unlocked"
    assert states(connection)["喝"] == "locked"
    learn(connection, "我")
    set_manual(connection, "好", version(connection, "words", "好"), manual=True)
    forget(connection, "我")
    assert states(connection)["好"] == "unlocked"
    assert states(connection)["很"] == "locked"
    set_manual(connection, "好", version(connection, "words", "好"), manual=False)
    assert states(connection)["好"] == "locked"


def test_skipped_sentences_do_not_unlock_words(connection: sqlite3.Connection) -> None:
    add(connection, SENTENCES)
    learn(connection, "我")
    set_hidden(connection, 1, version(connection, "sentences", "我很好"), hidden=True)
    assert states(connection, "sentences")["我很好"] == "locked"
    assert states(connection)["很"] == "locked"
    set_hidden(connection, 1, version(connection, "sentences", "我很好"), hidden=False)
    assert states(connection, "sentences")["我很好"] == "eligible"
    assert states(connection)["很"] == "unlocked"


def test_a_full_rebuild_matches_incremental_updates(
    connection: sqlite3.Connection,
) -> None:
    add(connection, SENTENCES)
    learn(connection, "我")
    learn(connection, "你")
    see(connection, "很")
    forget(connection, "你")
    words, sentences = states(connection), states(connection, "sentences")
    connection.execute("UPDATE words SET state = 'locked' WHERE state != 'seen'")
    connection.execute("UPDATE sentences SET state = 'locked'")
    refresh(connection)
    assert states(connection) == words
    assert states(connection, "sentences") == sentences
