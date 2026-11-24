import unittest

from mpd_jukebox import filters
from mpd_jukebox.filters import Filter, Invalid, Subject, Unsupported

from .helpers import song


def subject(uri='a/b.mp3', prio=0, last_modified=0, added=0, **tags):
    return Subject(song(uri, **tags).tags, uri, prio, last_modified, added)


def matches(expression: str, subj: Subject) -> bool:
    result = filters.parse(expression)
    assert isinstance(result, Filter), result
    return filters.evaluate(result, subj)


class ParseTestCase(unittest.TestCase):
    def test_single_and_multiple_clauses(self):
        result = filters.parse("((Artist == 'A') AND (Genre != 'B'))")
        assert isinstance(result, Filter)
        self.assertEqual(len(result.clauses), 2)
        self.assertEqual(result.clauses[0].tag, 'artist')
        self.assertEqual(result.clauses[1].op, filters.Op.NOT_EQUAL)

    def test_quotes_and_escapes(self):
        result = filters.parse('(Title == "it\\\'s \\"x\\"")')
        assert isinstance(result, Filter)
        self.assertEqual(result.clauses[0].value, 'it\'s "x"')

    def test_invalid(self):
        for expression in (
            '(Nope == "x")',
            '(Artist)',
            '(Artist ==)',
            '(Artist == x)',
            '(Artist == \'x")',
            "(Artist == 'x\\')",
            "(Artist like 'x')",
            "(Artist =~ '(')",
            '(prio >= x)',
            "(prio == '1')",
            "(file contains 'x')",
            "(modified-since 'yesterday')",
            "(Artist == ')",
        ):
            with self.subTest(expression):
                self.assertIsInstance(filters.parse(expression), Invalid)

    def test_fuzzy_is_unsupported(self):
        self.assertIsInstance(filters.parse("(Artist ~~ 'abba')"), Unsupported)

    def test_usable(self):
        self.assertIsNone(filters.usable(filters.parse('(bad)')))
        self.assertIsInstance(
            filters.usable(filters.parse("(Artist == 'x')")), Filter
        )


class EvaluateTestCase(unittest.TestCase):
    def test_operators(self):
        s = subject(artist='The Beatles')
        self.assertTrue(matches("(Artist == 'the beatles')", s))
        self.assertFalse(matches("(Artist == 'beatles')", s))
        self.assertTrue(matches("(Artist contains 'beat')", s))
        self.assertTrue(matches("(Artist starts_with 'THE')", s))
        self.assertFalse(matches("(Artist starts_with 'beat')", s))
        self.assertTrue(matches("(Artist != 'abba')", s))
        self.assertTrue(matches("(Artist =~ '^the b')", s))
        self.assertTrue(matches("(Artist !~ 'abba')", s))
        self.assertFalse(matches("(Artist !~ 'beat')", s))

    def test_normalisation(self):
        self.assertTrue(
            matches("(Artist == 'bjork')", subject(artist='Björk'))
        )

    def test_missing_tag(self):
        s = subject()
        self.assertFalse(matches("(Genre == 'x')", s))
        self.assertTrue(matches("(Genre != 'x')", s))
        self.assertTrue(matches("(Genre !~ 'x')", s))

    def test_multi_value(self):
        s = subject(genre=['Rock', 'Pop'])
        self.assertTrue(matches("(Genre == 'pop')", s))
        self.assertTrue(matches("(Genre != 'jazz')", s))
        # Negative operators must hold for every value.
        self.assertFalse(matches("(Genre != 'pop')", s))

    def test_any(self):
        s = subject(artist='A', genre='Jazz')
        self.assertTrue(matches("(any == 'jazz')", s))
        self.assertFalse(matches("(any == 'blues')", s))
        self.assertTrue(matches("(any != 'blues')", s))
        self.assertFalse(matches("(any != 'jazz')", s))

    def test_and(self):
        s = subject(artist='A', genre='Jazz')
        self.assertTrue(matches("((Artist == 'a') AND (Genre == 'jazz'))", s))
        self.assertFalse(matches("((Artist == 'a') AND (Genre == 'rock'))", s))

    def test_file_and_base(self):
        s = subject(uri='music/x.mp3')
        self.assertTrue(matches("(file == 'music/x.mp3')", s))
        self.assertFalse(matches("(file == 'music/X.mp3')", s))
        self.assertTrue(matches("(base 'music')", s))
        self.assertFalse(matches("(base 'other')", s))

    def test_dates_and_prio(self):
        s = subject(last_modified=1_700_000_000, added=100, prio=5)
        self.assertTrue(matches("(modified-since '2023-01-01')", s))
        self.assertFalse(matches("(modified-since '2024-01-01')", s))
        self.assertTrue(matches("(added-since '100')", s))
        self.assertFalse(matches("(added-since '101')", s))
        self.assertTrue(matches('(prio >= 5)', s))
        self.assertFalse(matches('(prio >= 6)', s))

    def test_audio_format_ignored(self):
        self.assertTrue(matches("(AudioFormat == '44100:16:2')", subject()))

    def test_album_subject_skips_song_only_clauses(self):
        album = Subject(song('x', album='A').tags)
        self.assertTrue(matches("(file == 'nothing')", album))
        self.assertTrue(matches("(base 'nothing')", album))
        self.assertTrue(matches('(prio >= 9)', album))


class PredicateTestCase(unittest.TestCase):
    def setUp(self):
        self.rock = subject(genre='Rock', artist='A')
        self.pop = subject(genre='Pop', artist='B')

    def flt(self, expression):
        result = filters.parse(expression)
        assert isinstance(result, Filter)
        return result

    def test_no_filters_include_all(self):
        self.assertTrue(filters.predicate(None, None)(self.rock))

    def test_include(self):
        check = filters.predicate(self.flt("(Genre == 'rock')"), None)
        self.assertTrue(check(self.rock))
        self.assertFalse(check(self.pop))

    def test_exclude_wins(self):
        check = filters.predicate(
            self.flt("(Genre == 'rock')"), self.flt("(Artist == 'a')")
        )
        self.assertFalse(check(self.rock))
        check = filters.predicate(None, self.flt("(Artist == 'a')"))
        self.assertTrue(check(self.pop))
