# Measure search quality

Find out whether a change to Jezero (a new source, a summarizer setting, a ranking
weight) makes search better or worse, with a fixed set of questions and a score you can
compare from one run to the next.

## Before you start

- A library with items in it, and the folder that holds `jezero.toml`. The commands below
  use `jezero`; run them as `.venv/bin/jezero` or `uv run jezero` if that is how you
  installed it.
- Questions you know the answer to, and a way to look up the items that hold each answer
  (`jezero search`, `jezero show ID` or the app).

## What the score measures

`jezero eval` runs each question through the same search the MCP server runs (BM25 with
the recency boost from `[search]`) and checks whether one of the items you named comes
back in the top 5. It does not run an agent: an agent rephrases a question, searches
more than once and calls `later`, so it usually does better. Read the score as a floor
that should not drop, not as the answer rate your agents get.

> [!NOTE]
> The question file names items from your library, so keep it out of git. Next to
> `jezero.toml`, in the `data/` folder, is a good place.

## Try the example

1. Load the demo items into an empty library and run the example file:

   ```bash
   jezero import-jsonl examples/items.example.jsonl --source demo
   jezero eval examples/eval.example.toml
   ```

   You should see:

   ```text
   ok   rollback              #1  How would we undo the invoices migration if it goes wrong?
   ok   flaky-refund          #1  Why does the refund test fail now and then?
   ok   docs-hosting          #1  ¿Dónde se publica la documentación y por qué no tiene ser...
   ok   frozen-time           #1  What did we change so the payments tests stop depending o...
   ok   after-rollback-plan   #1  What happened in billing-api after the rollback plan?

   5/5 found in the top 5  (hit@1 100%, hit@5 100%, MRR 1.00)
   ```

   `#1` is the position of the first expected item. `hit@1` is the share of questions
   answered by the first result, and `MRR` (mean reciprocal rank) rewards answers near
   the top: 1.00 means every answer came first.

## Write your question file

1. Copy the example next to your config:

   ```bash
   mkdir -p data/eval
   cp examples/eval.example.toml data/eval/questions.toml
   ```

2. Replace the cases with your own. Each `[[case]]` has an `id`, the `question` as
   someone would really ask it, and `expect`, the ids of the items that answer it:

   ```toml
   [[case]]
   id = "invoice-export-format"
   question = "Which format did we pick for the invoice export?"
   expect = [412, 415]
   ```

   Aim for 30 or more cases, taken from different sources and projects, and include a few
   decisions that changed later.

3. Name every item that states the answer in `expect`, not only the first one you found.
   Several sessions often repeat the same decision, and a short list counts a right
   answer as a miss. Run the file once, and for each `MISS` read the items it returned:
   add the ones that state the answer (not the ones that only talk about the subject).

4. For a decision that changed, add a `later` case: `item` is the item with the old
   state, `query` the words of the subject, and `expect` the items with the new state.
   It checks the `later` tool agents call before trusting a decision:

   ```toml
   [[case]]
   id = "export-format-changed"
   kind = "later"
   item = 380
   query = "invoice export format"
   question = "After choosing CSV, which format did we end up with?"
   expect = [415]
   ```

> [!TIP]
> Write questions in your own words. A question copied from an item's title always
> passes and tells you nothing.

## Compare before and after a change

1. Save a run before the change:

   ```bash
   jezero eval data/eval/questions.toml --save data/eval/before.json
   ```

2. Make the change (a new connector, a `[search]` weight, a re-summary), then compare:

   ```bash
   jezero eval data/eval/questions.toml --compare data/eval/before.json
   ```

   You should see the usual table, then one more line:

   ```text
   before: 5/5; better: -; worse: -
   ```

   `better` and `worse` list the case ids that started or stopped passing.

> [!WARNING]
> Do not tune the search on the same questions you measure it with: a change made to fix
> one case will pass that case whether or not it helps anywhere else. When a fix comes
> from a failing case, prove it on that case, then measure with new questions.

## Read with a client's sources

An agent only sees the sources its key allows. To score what one client sees, pass its
name from `[[clients]]`, or the source globs:

```bash
jezero eval data/eval/questions.toml --client laptop
jezero eval data/eval/questions.toml --sources "agent-*"
```

A case can also set its own `sources` or `project`.

## Fail a script when quality drops

`--fail-under` exits with `1` when the share of passing cases is below a value from 0 to
1, so a script or a timer can stop on it:

```bash
jezero eval data/eval/questions.toml --fail-under 0.8
```

You should see, when it is below:

```text
below the target: hit@5 0% < 80%
```

Set it once in the file with `fail_under = 0.8` under `[defaults]`.

## When a case names an item that is gone

Items can disappear (a segment pruned when its session was rewritten, a source reset).
The case then shows `stale key: [ID]` and the totals line says how many cases need a new
`expect`. Find the item that holds the answer now and update the list.

## Next steps

- [Add and tune sources](sources.md): run the file before and after piloting a new one.
- [How search works](../explanation/search.md): why some questions miss.
- [`jezero eval`](../reference/cli.md#jezero-eval): every option.
