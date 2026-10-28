# Yiju guide for agents

Yiju is a self-hosted app for learning Chinese vocabulary through sentences. You
help by writing sentences and putting them on the check screen. You never add
anything to the library yourself, and you never change or delete anything in
it. The user approves sentences in the web app by choosing the ones they want
to learn. The CLI has no command for approving.

## Getting it right is your job

Assume the user approves everything you stage without checking it. They are a
learner and cannot tell a wrong split, a wrong reading, a wrong meaning or a
wrong translation from a right one, so whatever you stage is what they learn.
`ready: true` only means the format checks passed. It says nothing about
whether the Chinese, the pinyin, the meanings or the English are right.

- Stage only what the user asked for or agreed to. Suggest, then wait for a
  yes before you stage.
- Check every word of every item you stage against "Check it yourself" before
  you tell the user it is waiting.
- When something cannot be made right, such as a word whose dictionary entry
  has no sense that fits, change the sentence instead of staging a wrong one,
  and tell the user what you changed and why.
- When you talk to the user about Chinese, put the pinyin next to every word
  or sentence, as in 小心 (xiǎoxīn). Do not assume they can read characters.
- Tell the user about anything you were unsure of instead of guessing quietly.

## Running the CLI

Run the CLI from a checkout of the repository and point it at the user's
server:

```sh
export YIJU_URL=http://the-server:8000
nix develop --option warn-dirty false -c uv run yiju stats
```

`uv run yiju stats` works too if uv is on your PATH. Keep stderr visible, so
you see warnings and usage errors.

Without `YIJU_URL`, the CLI uses `http://127.0.0.1:8000`. `--url` overrides
both for one command. It goes after the command, as in
`yiju stats --url http://the-server:8000` or
`yiju import show --url http://the-server:8000`.

Every command prints JSON on stdout. When the server rejects a request, the
command prints `{"error": "..."}` and exits with status 1. A mistake in the
command line itself, such as an unknown command or option, prints a usage
message on stderr instead and exits with status 2.

The checkout and the server can be on different versions. If a command you
expect to work fails because the server does not know the route, or the CLI
does not know the command, tell the user. Do not work around it.

## The workflow

1. Run `yiju sentences` and `yiju import show` to see what is already in the
   library and what is already waiting, so you do not repeat a sentence.
2. Write sentences in the import format below.
3. Stage them with `yiju import add`. The output shows the sentences you just
   staged, the new words they bring, and every issue on the check screen. Keep
   the IDs of your sentences. The check screen shows nothing about who staged
   an item, so the IDs are how you tell yours apart.
4. Fix every issue with the edit commands until `ready` is `true`.
5. Go through the list in "Check it yourself" for every item you staged. The
   check screen only catches format problems, so `ready` does not mean the
   sentences are right. Fix what you find.
6. Tell the user that items are waiting on the check screen. Its address is the
   server's URL followed by the `check_screen` path, such as
   `http://the-server:8000/pending`.

Staged items stay until the user approves or discards them. Your items share
the check screen with anything else waiting there, so do not remove items you
did not add.

## Writing sentences

- Write natural Chinese, the way a native speaker would say it, with a natural
  English translation.
- Every sentence should be worth studying for a long time. Prefer words and
  patterns the user will keep meeting in what they want to read, watch or
  listen to. Leave out filler.
- Keep the translation in English only.
- When a sentence refers to a person by name or with 他 or 她, write that
  person as a slot, described in "People and names". 他们 and 她们 are normal
  words.

## Check it yourself

The check screen reports only what it can test by rule: the format, the word
split spelling out the sentence, the pinyin having one group per word,
missing definitions, slots and gender rules that do not add up, and repeats.
Everything below is your job. Check every item you staged thoroughly before you
tell the user, and read each word's `meanings` in full.

- **The word split.** Each entry in a sentence's `words` should be one real
  word, the way this sentence uses it. Automatic segmentation sometimes gets
  this wrong, so check every word it gives. A split from automatic
  segmentation is fine when every word in it is a real word with a meaning that
  fits the sentence. Fix a wrong split with spaces in the Chinese when you stage
  the sentence, or with `yiju import edit-sentence ID --words "..."` afterwards.
  `yiju word WORD...` shows the CC-CEDICT entry for any word, and takes several
  words at once, so you can check whether a longer stretch of characters is one
  dictionary word.

  Having a dictionary entry does not make a stretch of characters one word
  for this app. The question is whether a learner who knows the parts could
  work out what the whole means. Split a dictionary word into its parts when
  each part's own meaning fits the sentence and together they plainly say
  what the whole means, as in 闭嘴 (bì zuǐ, "close mouth") or 门外 (mén
  wài, "door outside"). Keep it whole when the whole means something the
  parts do not give you, as in 东西 (dōngxi, "thing", not "east west") or
  小心 (xiǎoxīn, "careful", not "small heart"). Also keep these whole:
  - words where one part already carries the whole meaning, such as 告诉
    (gàosu), 喜欢 (xǐhuan) and 意思 (yìsi)
  - doubled words, such as 谢谢 (xièxie) and 明明 (míngmíng)
  - words with a part that rarely stands on its own, such as 这样 (zhèyàng),
    这里 (zhèlǐ) and 师兄 (shīxiōng)
  - words whose parts each have many meanings, where the one that fits is
    hard to find, such as 宗门 (zōngmén)
  When you are not sure, keep it whole.
- **The readings.** Each word in a sentence shows the `meanings` of the reading
  its pinyin gives it. Read them against the sentence. When a word has more
  than one reading, `choices` lists them all, and the generated pinyin can pick
  the wrong one. Fix the pinyin with
  `yiju import edit-sentence ID --pinyin "..."`.
- **People.** Look for a literal 他 or 她, and for any name written out. The
  check screen does not flag either. Replace them with slots by removing the
  sentence and staging it again.
- **Pronouns and rules.** Each pronoun slot in the translation should point at
  the right person, and its verb should go with he or she. The gender rules
  should match what the sentence says about its people.
- **The translation.** It should say what the Chinese says.
- **The meanings.** Every word in every sentence you stage, new or already in
  the library, must have a sense in its `meanings` that matches how this
  sentence uses it. A sense far down the list is fine. A sense that is only
  close is not. When no sense fits, split differently or reword the sentence.
- **New words.** Every word in the output's `words` list is about to join the
  library. Make sure each one is a real word and its `meanings` include the way
  the sentence uses it. A wrong split often still has a definition, just
  one that does not fit. "has no definition" usually means the split is wrong.
  Do not trim long definitions. The user learns which sense applies from the
  sentences. If a dictionary definition looks wrong or misleading, leave it as
  it is and tell the user which word it is and what looks wrong.
- **The names you staged.** Check the pinyin, the English spelling and the
  gender of each one.

### Writing the words and the pinyin

`--words` lists the sentence's words separated by spaces, in order, without
punctuation. Write slots the way they appear in the Chinese, such as `{A}` and
`{A.他}`. The words must spell out the sentence's characters exactly. The
command fails and changes nothing if they do not. A new split regenerates the
pinyin, so when you change the split and a reading, pass `--words` and
`--pinyin` in the same command.

`--pinyin` has one group per word or slot, separated by spaces, in the same
order as `words`. Write a word of several syllables as one group, without
spaces inside it. Write a name slot as `{A}` and a pronoun slot as `tā`.
Punctuation stays attached to the groups around it. Use the tones the dictionary
gives each word, without tone changes such as 不 before a fourth tone. So 不
is always bù, 一 is always yī, and two third tones in a row both stay third,
as in nǐ hǎo. Keep the neutral tones CC-CEDICT gives, as in gàosu and
xièxie, and use neutral qilai and chulai when they follow a verb. The
generated pinyin follows most of this, so edit only the groups that are wrong.

No command changes a sentence's Chinese. To change it, remove the sentence
with `yiju import remove-sentence ID` and stage the corrected line again.

A new word keeps every reading CC-CEDICT has for it, so the sentence pinyin is
the place to fix a reading. For a word CC-CEDICT does not have, write a
definition with `yiju import edit-word WORD --meanings "..."`. Its `--pinyin`
sets the reading that goes with your meanings. Write your own definition only
when CC-CEDICT has no entry and the word cannot be split, such as 修为
(xiūwéi) in cultivation novels, and tell the user which words have one.

## The import format

This is the same text `yiju import format` prints, in its `format` field:

```text
One item per line. Blank lines and lines starting with # are ignored.

A sentence is the Chinese, then a tab or " | ", then the English translation:
    <Chinese><TAB><English>
    <Chinese> | <English>

Spaces inside the Chinese mark word boundaries and replace automatic
segmentation for that sentence. A space next to punctuation changes nothing.
The spaces around " | " belong to the separator:
    <word> <word> <word> | <English>

A line with only Chinese words, separated by spaces, adds those words
without a sentence:
    <word> <word>

A person is a slot, which study fills with a random name from the Names tab.
Write {A}, {B} and so on where the names go, with the same letters in the
Chinese and the translation.

{A.他} becomes 他 or 她 to match A. In the translation, {A.they}, {A.them},
{A.their}, {A.theirs} and {A.themself} become he or she, him or her and so
on. A capital letter carries over, so {A.They} starts a sentence. Write the
verb that goes with he or she, as in "{A.They} is".

An optional third part sets gender rules, separated by spaces: A=male,
A=female, A=B for the same gender and A!=B for different genders:
    <Chinese> | <English> | A=male B!=A
```

An import is all or nothing. If any line is malformed, nothing from it is
staged. Fix the lines the error lists and send the whole text again.

## People and names

Every time a sentence card is shown, study picks new names for its slots. Each
letter gets a different name, so a sentence with two letters needs two names
that fit its rules. Sentences with slots are fine to add before there are any
names.

- `{A}` is a name. In the translation it becomes the English spelling.
- `{A.他}` is the pronoun for A. `{A.她}` means the same thing.
- In the translation, `{A.they}` becomes he or she, `{A.them}` him or her,
  `{A.their}` his or her, `{A.theirs}` his or hers and `{A.themself}` himself
  or herself.
- The English always comes out singular, so write the verb that goes with he
  or she: `{A.They} is`, `{A.They} has`.
- The Chinese and the English can use pronouns in different places. English
  may need `{A.their}` where the Chinese has no pronoun, and the other way
  round.
- A letter can appear only as a pronoun, with no `{A}` anywhere. Every letter
  in the Chinese must also appear in the translation, and the other way round.
- Use at most six people in one sentence.

Gender rules say what the sentence fixes about its people. `A=male` and
`A=female` fix a gender. `A=B` and `A!=B` tie two people together. Without
rules each person can be either gender.

### The names

Names are a separate list that the user also approves on the check screen.
Stage names only when the user asks for them. Stage a name only when you have
confirmed its gender from a source, such as the pronouns used for the
character in the book or a wiki. Leave out names whose gender you could not
confirm, and tell the user which ones. `yiju names list` shows the
approved names. Stage new ones with `yiju names add`, in this format, which
`yiju names format` also prints in its `format` field:

```text
One name per line: the full name with the surname first, a space, then male
or female.
    <characters> male
    <characters> female

Yiju fills in the pinyin and the English spelling. Both can be changed on the
check screen.
```

Fix a name with `yiju names edit ID --pinyin ... --english ...`.

## Changing the library

Sentences and words the user already approved can be wrong too, such as a
split that does not follow "The word split". You fix them the same way you
add things: propose the change, and it waits on the check screen until the
user approves it. Until then the library stays as it is. Propose a change only
when the user asks for it or agrees to it.

- `yiju library edit-sentence ID` proposes a new split, pinyin, translation or
  rules for library sentence ID, with the same options and rules as
  `yiju import edit-sentence`. The Chinese cannot change. The sentence keeps
  its cards and their review history. Words the new split brings in join the
  library with it, and appear in `words` like the words of a new sentence.
  If the new split brings in a word the user has not studied yet, the sentence
  leaves study until they have seen that word.
- `yiju library edit-word WORD --meanings "..."` proposes your own definition
  for a library word. Follow the same rule as for new words: leave the
  dictionary definition alone unless it is missing or wrong, and tell the user
  why you changed it.
- `yiju library remove-sentence ID` and `yiju library remove-word WORD`
  propose deleting a sentence or a word together with its cards and all of its
  review history. The output's `reviews` says how many reviews would go.
  Deleting cannot be undone, so propose it only when the user asks for it, or
  for a word a new split leaves behind, as described below.
  A word can be removed only once no sentence uses it, counting the changes
  waiting with it. When only waiting changes take it out of its sentences, the
  removal has `waits_on`, the IDs of those changes, and the user has to approve
  them before the removal. Tell the user which ones to approve first.

A split that drops a word does not remove the word. It stays in the library
with its cards and history. So whenever a new split drops a word, also propose
`yiju library remove-word` for it, unless another sentence still uses it. Run
`yiju import show` afterwards. If the removal has an issue saying the word is
still in another sentence, the word is used elsewhere and should stay, so drop
the removal with `yiju import remove-change ID`. Otherwise the removal has
`waits_on`, and you tell the user to approve the split before the removal.

Proposing again for the same sentence or word updates the waiting change.
Options you leave out keep what you proposed before, or the library's value.
`yiju import remove-change ID` drops a change you proposed.

Check a change the same way as a new sentence: read the `after` words, their
`pinyin` and `meanings`, and the new words it brings, before you tell the user
it is waiting.

## Command reference

Library commands only read.

| Command | Prints |
| --- | --- |
| `yiju stats` | Counts of words and sentences by status, today's queue, and in `pending` one total of the sentences, word lines, names and library changes waiting on the check screen |
| `yiju words [--status S] [--flagged] [--backlog]` | Every word with pinyin, a short meaning and its status |
| `yiju word WORD...` | A library word with its readings, strength, cards and sentences. Any other word gets its CC-CEDICT readings and `"status": "not in the library"`. Several words print a list, with an `error` entry for a word that is in neither |
| `yiju sentences [--status S]` | Every sentence with its ID, Chinese, pinyin, translation, rules, status, `skipped`, `needs_names` and words |
| `yiju flags` | Words the user flagged as wrong, with their notes |
| `yiju reviews [--since DATE]` | The review log |
| `yiju dictionary [--update]` | The date of the CC-CEDICT copy |
| `yiju names list` | The approved names |

Check screen commands stage and revise. Each one prints the check screen
narrowed to the items the command touched, with the issues, `ready` and `adds`
for the whole check screen. `yiju import show` prints everything.

| Command | Does | Shows |
| --- | --- | --- |
| `yiju import add [FILE]` | Stages sentences and words from FILE or stdin | The staged sentences and their new words |
| `yiju import show` | Changes nothing | Everything waiting |
| `yiju import edit-sentence ID [--words W] [--pinyin P] [--translation T] [--rules R]` | Revises one sentence. An empty `--rules ""` clears the rules | The sentence and its new words |
| `yiju import edit-word WORD --meanings M [--pinyin P]` | Writes a definition for a new word CC-CEDICT does not have. Separate meanings with a new line or a typed `\n`. An empty `--meanings ""` removes your definition | The word with its readings, and the sentences that use it |
| `yiju import remove-sentence ID` | Drops a sentence | Only the summary |
| `yiju import remove-word WORD` | Drops a word that is waiting on its own line. A word from a sentence goes with its sentence | Only the summary |
| `yiju names add [FILE]` | Stages names from FILE or stdin | The staged names |
| `yiju names edit ID [--pinyin P] [--english E] [--gender male\|female]` | Revises a waiting name | The name |
| `yiju names remove ID` | Drops a waiting name | Only the summary |
| `yiju library edit-sentence ID [--words W] [--pinyin P] [--translation T] [--rules R]` | Proposes a change to library sentence ID | The change and its new words |
| `yiju library edit-word WORD --meanings M [--pinyin P]` | Proposes your own definition for a library word | The change |
| `yiju library remove-sentence ID` | Proposes deleting a library sentence with its cards and history | The change |
| `yiju library remove-word WORD` | Proposes deleting a library word with its cards and history | The change |
| `yiju import remove-change ID` | Drops a waiting change | Only the summary |

### The check screen output

- `sentences` lists sentences with their `id`, `chinese`, `pinyin`,
  `translation`, `rules`, `people`, `duplicate`, `needs_names`, `issues`,
  `starts` and `words`. `starts` is the state the sentence starts in once
  approved: `locked`, `eligible` or `unlocked`. Each entry in `words` has its `text`. A slot also has `"slot": true`
  and nothing else. Any other word has the `pinyin` this sentence gives it,
  `new` and `meanings`, the meanings of the reading that pinyin gives.
  `meanings` is null when the pinyin matches none of the word's readings. A
  word with more than one reading also has `choices`, which lists every
  reading with its `pinyin` and first `meaning`. A word already in the
  library has its `status`, such as `learning` or `locked`, and `null` when it
  is new.
- `words` lists new words with their `word`, `found`, `requested`, `manual`,
  `sentences` and `changes`. `found` is false when CC-CEDICT has no entry.
  `requested` is true for a word on its own line. `manual` is true when it has
  your own definition. `sentences` lists the IDs of the waiting sentences it is
  in, and `changes` the IDs of the library changes that bring it in. A word in
  neither, or one you just edited, also has its `readings`. Each
  reading has a `pinyin` and a list of `meanings`, and `classifiers` and
  `also_pronounced` when the dictionary gives them. `yiju word` shows these for
  any word.
- `names` lists names with their `id`, `chinese`, `pinyin`, `english`,
  `gender`, `duplicate` and `issues`.
- `changes` lists library changes with their `id`, `kind` (`sentence` or
  `word`), `target` (the sentence ID or the word), `action` (`edit` or
  `remove`) and `issues`. A sentence change also has its `chinese`. A sentence
  edit has `before` and `after`, each with `words`, `pinyin`, `translation`
  and `rules`. The `after` words have the same fields as a waiting sentence's
  words. A word edit has `before` and `after` readings. A removal has
  `reviews`, the number of reviews it would delete, and the sentence's
  `translation` or the word's `readings`. A word removal that waits on other
  changes also has `waits_on`.
- `issues` lists every problem that blocks approval, anywhere on the check
  screen. A sentence's problems start with its ID, as in `Sentence 2: ...`,
  and a change's with its change ID, as in `Change 1: ...`. `problems` has
  the same issues grouped by the tab of the check screen they appear on:
  `sentences`, `words`, `names`, `edits` and `removals`.
- `ready` is `true` when nothing blocks approval and there is something to
  add or change. An empty check screen is not ready.
- `adds` counts the sentences, words and names that approving would add, and
  in `changes` the library changes it would make.
- `duplicate: true` on a sentence or name means it is already in the library
  and will not be added again.
- `already_waiting` appears after `yiju import add` and `yiju names add`. It
  lists what you sent that was already on the check screen, and is empty when
  nothing was. Yiju kept the waiting copy unchanged and shows it. Edit that one
  if yours was different.
- `needs_names` is set when the approved and waiting names cannot fill a
  sentence's slots yet. It says how many names the sentence uses, such as
  `"2 names"` or `"1 male name"`.
- `people` is true when a sentence has slots or rules. `version` is for the
  web app, and you can ignore it.

### Errors

- `{"error": "The text is not in the import format.", "lines": [...]}` lists
  each bad line by number. Nothing was staged.
- `{"error": "There is no pending sentence 99."}` means the ID is wrong or the
  user already approved or discarded the item. Run `yiju import show` again.
- `{"error": "The words must spell out the sentence's characters in order."}`
  means `--words` did not match the Chinese. Nothing changed.
- `{"error": "There is no sentence 99 in your library."}` and
  `{"error": "茶 is not in your library."}` mean a `yiju library` command named
  something the library does not have. Check `yiju sentences` or `yiju word`.
- `{"error": "Could not reach yiju at ..."}` means the server is down or
  `YIJU_URL` is wrong.
