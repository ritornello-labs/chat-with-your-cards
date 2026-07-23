# User stories

These are the product moments the public screenshots and scripted demo
collection are designed to show.

## Explain the card in front of me

> As a learner who has reached a difficult card, I want to ask a question
> without restating the card, so I can repair my understanding without leaving
> the reviewer.

The assistant receives the current reviewer card as context and explains the
quantifier change between pointwise and uniform continuity.

## Find the missing prerequisite

> As a learner who can repeat a fact but cannot see why it is true, I want the
> assistant to search my own collection for prerequisite cards, so I can review
> the smallest useful path instead of browsing an entire deck.

The assistant searches a synthetic analysis collection, identifies compactness
as the missing bridge, and proposes a three-card review order.

## Turn a confusion into a reviewable card

> As a learner who has identified a precise misconception, I want the assistant
> to propose a focused companion card, so I can inspect and edit it before
> anything is written to my collection.

The assistant proposes a new note about quantifier order. The screenshot leaves
the proposal pending, making the review-before-write boundary visible.

## Demo-fixture rule

The corresponding collection is synthetic and created only inside the
disposable `anki-addon-workbench` profile. Public captures must never depend on
or display a real user collection.
