/* Chat With Your Cards - webview UI.
 *
 * Renders the chat transcript and forwards user intent to Python via
 * pycmd("cwyc:" + JSON). Python pushes events through chatUI.dispatch().
 * Event vocabulary mirrors backends/base.py plus UI-directed messages
 * ("reset", "cancelled", "proposal", "ledger", "pins", ...). Unknown
 * types are ignored (forward-compatible).
 */
(function () {
    "use strict";

    var transcript = null;
    var input = null;
    var sendButton = null;

    var streaming = false;
    var currentAssistant = null; // {element, markdown}
    var toolChips = {}; // call_id -> element
    var pinnedToBottom = true;

    var proposals = {}; // id -> {data, root, fieldInputs, included, order}
    var proposalOrder = [];
    var activeProposalId = null;
    var revisingProposalId = null; // proposal a "Suggest change" targets until sent
    var pins = { deck: "", note_type: "", tags: [], fields: {} };
    var collectionMeta = { decks: [], note_types: [], tags: [] };
    var pinTags = []; // committed tag chips in the pins panel

    function post(msg) {
        if (typeof pycmd === "function") {
            pycmd("cwyc:" + JSON.stringify(msg));
        }
    }

    function renderMarkdown(markdown) {
        if (typeof marked !== "undefined" && marked.parse) {
            return marked.parse(markdown);
        }
        var div = document.createElement("div");
        div.textContent = markdown;
        return div.innerHTML;
    }

    function scrollToBottomIfPinned() {
        if (pinnedToBottom) {
            transcript.scrollTop = transcript.scrollHeight;
        }
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined) {
            node.textContent = text;
        }
        return node;
    }

    function addUserMessage(text) {
        var row = el("div", "cwyc-row cwyc-row-user");
        var bubble = el("div", "cwyc-msg cwyc-msg-user msg-user", text);
        row.appendChild(bubble);
        transcript.appendChild(row);
        scrollToBottomIfPinned();
    }

    function startAssistantMessage() {
        var row = el("div", "cwyc-row cwyc-row-assistant");
        var body = el("div", "cwyc-msg cwyc-msg-assistant msg-assistant cwyc-streaming");
        row.appendChild(body);
        transcript.appendChild(row);
        currentAssistant = { element: body, markdown: "" };
        scrollToBottomIfPinned();
    }

    function appendDelta(text) {
        if (!currentAssistant) {
            startAssistantMessage();
        }
        currentAssistant.markdown += text;
        currentAssistant.element.innerHTML = renderMarkdown(currentAssistant.markdown);
        scrollToBottomIfPinned();
    }

    function breakAssistantBlock() {
        // The next text deltas belong to a fresh assistant block; the block
        // before this row is finished, so stop its cursor.
        if (currentAssistant) {
            currentAssistant.element.classList.remove("cwyc-streaming");
        }
        currentAssistant = null;
    }

    // Friendly, non-technical labels for the collection tools. Anything not
    // listed here (the CLI's own ToolSearch, propose_note whose proposal card
    // IS the visible feedback) is hidden so the transcript stays readable.
    var TOOL_LABELS = {
        search_notes: "Searched your cards",
        get_note: "Read a note",
        get_card: "Read a card",
        deck_tree: "Looked through your decks",
        tag_tree: "Looked through your tags",
        collection_stats: "Checked your collection stats",
        list_note_types: "Listed your note types",
        get_note_type: "Checked a note type",
        find_related: "Found related cards",
        get_card_images: "Looked at the card's images",
    };

    function friendlyTool(raw) {
        var match = /^mcp__anki__(.+)$/.exec(raw || "");
        if (!match) {
            return null; // internal CLI tool (e.g. ToolSearch) — hide it
        }
        return TOOL_LABELS[match[1]] || null; // unmapped (propose_*) — hide
    }

    function toolHint(rawInput) {
        try {
            var obj = JSON.parse(rawInput);
            if (obj.query) return obj.query;
            if (obj.name) return obj.name;
            if (obj.prefix) return obj.prefix;
            if (obj.note_id) return "note " + obj.note_id;
            if (obj.card_id) return "card " + obj.card_id;
        } catch (err) {
            /* compacted/truncated input — no hint */
        }
        return "";
    }

    function toolDetailBlock(label, text) {
        text = (text || "").trim();
        if (!text) {
            return null;
        }
        var block = el("div", "cwyc-tool-detail-block");
        block.appendChild(el("div", "cwyc-tool-detail-label", label));
        block.appendChild(el("pre", "cwyc-tool-detail-body", text));
        return block;
    }

    var CARET_SVG =
        '<svg class="cwyc-tool-caret" viewBox="0 0 16 16" width="9" height="9" ' +
        'aria-hidden="true"><path d="M4 6l4 4 4-4" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" fill="none"/></svg>';

    function addToolChip(callId, tool, summary) {
        var label = friendlyTool(tool);
        if (!label) {
            return; // hidden tool: no chip, and don't break the text flow
        }
        var chip = el("div", "cwyc-tool-chip tool-chip cwyc-tool-running");
        var head = el("div", "cwyc-tool-head");
        head.innerHTML =
            '<span class="cwyc-tool-spinner"></span>' +
            '<span class="cwyc-tool-name"></span>' +
            '<span class="cwyc-tool-summary"></span>' +
            '<span class="cwyc-tool-result"></span>' +
            CARET_SVG;
        head.querySelector(".cwyc-tool-name").textContent = label;
        head.querySelector(".cwyc-tool-summary").textContent = toolHint(summary);
        var details = el("div", "cwyc-tool-details");
        details.hidden = true;
        var inputBlock = toolDetailBlock("Input", summary);
        if (inputBlock) {
            details.appendChild(inputBlock);
        }
        // Collapsed by default; the whole header row is the toggle. Only
        // expandable once there is something (input args, then the result).
        head.addEventListener("click", function () {
            if (!details.children.length) {
                return;
            }
            var open = chip.classList.toggle("cwyc-tool-expanded");
            details.hidden = !open;
            scrollToBottomIfPinned();
        });
        chip.appendChild(head);
        chip.appendChild(details);
        var row = el("div", "cwyc-row cwyc-row-tool");
        row.appendChild(chip);
        transcript.appendChild(row);
        toolChips[callId] = chip;
        breakAssistantBlock();
        scrollToBottomIfPinned();
    }

    function finishToolChip(callId, ok, resultSummary) {
        var chip = toolChips[callId];
        if (!chip) {
            return; // hidden tool, or an unknown call id
        }
        chip.classList.remove("cwyc-tool-running");
        chip.classList.add(ok ? "cwyc-tool-ok" : "cwyc-tool-failed");
        // The check/✗ marker summarizes at a glance; the raw result is tucked
        // into the expandable detail area for when the user wants specifics.
        var details = chip.querySelector(".cwyc-tool-details");
        var block = toolDetailBlock(ok ? "Result" : "Error", resultSummary);
        if (details && block) {
            details.appendChild(block);
        }
        scrollToBottomIfPinned();
    }

    function finalizeStream(stopped) {
        if (currentAssistant) {
            currentAssistant.element.classList.remove("cwyc-streaming");
            if (stopped) {
                var note = el("div", "cwyc-stopped-note", "Stopped");
                currentAssistant.element.appendChild(note);
            } else if (!currentAssistant.markdown.trim()) {
                var empty = el(
                    "div",
                    "cwyc-stopped-note",
                    "No response received - see user_files/logs/backend.log."
                );
                currentAssistant.element.appendChild(empty);
            }
        }
        currentAssistant = null;
        setStreaming(false);
    }

    function setStreaming(value) {
        streaming = value;
        sendButton.classList.toggle("cwyc-streaming-btn", value);
        sendButton.title = value ? "Stop (Esc)" : "Send (Enter)";
        sendButton.setAttribute("aria-label", value ? "Stop" : "Send");
    }

    function sendCurrentInput() {
        var text = input.value.trim();
        if (!text || streaming) {
            return;
        }
        input.value = "";
        autosizeInput();
        suggestionIndex += 1;
        refreshSuggestion();
        addUserMessage(text);
        startAssistantMessage();
        setStreaming(true);
        post({ type: "send", text: text });
        supersedeRevisingProposal();
    }

    // "Suggest change" pre-fills the composer with a revision request; sending
    // it means the original card is being replaced, so set it aside (superseded,
    // restorable) instead of leaving it dangling as pending. No-op if it was
    // already resolved.
    function supersedeRevisingProposal() {
        var id = revisingProposalId;
        revisingProposalId = null;
        if (!id) {
            return;
        }
        var record = proposals[id];
        if (record && record.data.status === "pending") {
            post({ type: "proposal_supersede", id: id });
            markResolved(id, "superseded", {});
        }
    }

    function resetTranscript() {
        transcript.innerHTML = "";
        toolChips = {};
        proposals = {};
        proposalOrder = [];
        activeProposalId = null;
        revisingProposalId = null;
        currentAssistant = null;
        pinnedToBottom = true;
        renderLedger({ entries: [] });
        updateBulkBar();
        setStreaming(false);
    }

    function autosizeInput() {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 160) + "px";
    }

    /* ---- word-level diff (LCS on word tokens) ---- */

    function diffTokens(text) {
        return text.match(/\S+\s*/g) || [];
    }

    function wordDiff(oldText, newText) {
        var a = diffTokens(oldText);
        var b = diffTokens(newText);
        if (a.length * b.length > 250000) {
            // Fields long enough to blow the DP table get a block diff.
            return [{ op: "del", text: oldText }, { op: "ins", text: newText }];
        }
        var rows = a.length + 1;
        var cols = b.length + 1;
        var lcs = new Array(rows * cols).fill(0);
        for (var i = a.length - 1; i >= 0; i--) {
            for (var j = b.length - 1; j >= 0; j--) {
                lcs[i * cols + j] = a[i].trim() === b[j].trim()
                    ? lcs[(i + 1) * cols + j + 1] + 1
                    : Math.max(lcs[(i + 1) * cols + j], lcs[i * cols + j + 1]);
            }
        }
        var ops = [];
        var x = 0;
        var y = 0;
        function pushOp(op, text) {
            var last = ops[ops.length - 1];
            if (last && last.op === op) {
                last.text += text;
            } else {
                ops.push({ op: op, text: text });
            }
        }
        while (x < a.length && y < b.length) {
            if (a[x].trim() === b[y].trim()) {
                pushOp("eq", b[y]);
                x++;
                y++;
            } else if (lcs[(x + 1) * cols + y] >= lcs[x * cols + y + 1]) {
                pushOp("del", a[x]);
                x++;
            } else {
                pushOp("ins", b[y]);
                y++;
            }
        }
        while (x < a.length) {
            pushOp("del", a[x++]);
        }
        while (y < b.length) {
            pushOp("ins", b[y++]);
        }
        return ops;
    }

    function renderDiff(container, oldText, newText) {
        container.innerHTML = "";
        wordDiff(oldText, newText).forEach(function (part) {
            var span = el(
                "span",
                part.op === "ins" ? "cwyc-diff-ins" : part.op === "del" ? "cwyc-diff-del" : "",
                part.text
            );
            container.appendChild(span);
        });
    }

    /* ---- card preview (real templates, rendered in a sandboxed iframe) ---- */

    function previewSrcdoc(side, css) {
        var night = document.documentElement.classList.contains("night-mode") ||
            document.body.classList.contains("nightMode");
        return "<!doctype html><html><head><meta charset='utf-8'><style>" +
            (css || "") +
            "\nhtml{overflow:auto;}body{margin:10px;}" +
            "</style></head><body class=\"card" +
            (night ? " nightMode night_mode" : "") +
            "\">" + side + "</body></html>";
    }

    function buildPreviewTabs(previews, startIndex, onSelect) {
        // Tabs: for edits Before/After (answer side); for creations Front/Back.
        var wrap = el("div", "cwyc-preview");
        var tabs = el("div", "cwyc-preview-tabs");
        var frame = document.createElement("iframe");
        frame.className = "cwyc-preview-frame";
        frame.setAttribute("sandbox", "");
        var options = [];
        if (previews.before && previews.after) {
            options = [
                { label: "Before", html: previews.before.answer, css: previews.before.css },
                { label: "After", html: previews.after.answer, css: previews.after.css },
            ];
        } else if (previews.after) {
            options = [
                { label: "Front", html: previews.after.question, css: previews.after.css },
                { label: "Back", html: previews.after.answer, css: previews.after.css },
            ];
        }
        if (!options.length) {
            return null;
        }
        // Default to the interesting side: After for edits, Back for creations.
        var active = typeof startIndex === "number" ? startIndex : 1;
        if (active < 0 || active >= options.length) {
            active = options.length - 1;
        }
        var buttons = options.map(function (option, index) {
            var button = el("button", "cwyc-preview-tab", option.label);
            button.type = "button";
            button.addEventListener("click", function () {
                buttons.forEach(function (b) { b.classList.remove("cwyc-active"); });
                button.classList.add("cwyc-active");
                frame.srcdoc = previewSrcdoc(option.html, option.css);
                if (onSelect) {
                    onSelect(index);
                }
            });
            tabs.appendChild(button);
            return button;
        });
        buttons[active].classList.add("cwyc-active");
        frame.srcdoc = previewSrcdoc(options[active].html, options[active].css);
        wrap.appendChild(tabs);
        wrap.appendChild(frame);
        return wrap;
    }

    /* ---- proposal cards ---- */

    function proposalStatusLabel(status) {
        return {
            "pending": "Pending review",
            "accepted": "Accepted",
            "auto-accepted": "Applied",
            "rejected": "Rejected",
            "undone": "Undone",
            "superseded": "Superseded",
        }[status] || status;
    }

    var KIND_BADGES = {
        create: "New note",
        edit: "Edit note",
        bulk: "Bulk operation",
        delete: "Delete notes",
        change_set: "Change set",
        deck_op: "Deck change",
        skill_update: "Skill update",
    };

    function bulkOpLabel(data) {
        if (data.kind === "delete") {
            return "Delete " + data.count + " note(s)";
        }
        var op = data.op || "";
        var a = data.op_args || {};
        if (op === "rename_tag") {
            return 'Rename tag "' + a.old_tag + '" → "' + a.new_tag + '" — ' +
                data.count + " note(s)";
        }
        if (op === "find_replace") {
            return 'Replace "' + a.search + '" → "' + a.replacement + '"' +
                (a.field ? " in " + a.field : "") + " — " + data.count + " note(s)";
        }
        if (op === "move_cards") {
            return "Move " + data.count + ' card(s) → "' + a.deck + '"';
        }
        if (op === "create_deck") {
            return 'Create deck "' + a.name + '"';
        }
        if (op === "rename_deck") {
            return 'Rename deck "' + a.old + '" → "' + a["new"] + '"';
        }
        if (op === "set_deck_options") {
            return 'Change ' + data.count + ' option(s) for deck "' + a.deck + '"';
        }
        if (op === "create_filtered_deck") {
            return 'Create filtered deck "' + a.name + '"';
        }
        if (op === "update_filtered_deck") {
            return 'Reconfigure filtered deck "' + a.deck + '"';
        }
        if (op === "filtered_deck_action") {
            return (a.action === "empty" ? "Empty" : "Rebuild") +
                ' filtered deck "' + a.deck + '"';
        }
        return data.count + " item(s)";
    }

    function renderBulkBody(record, body) {
        var data = record.data;
        body.appendChild(el("div", "cwyc-bulk-op", bulkOpLabel(data)));
        (data.samples || []).forEach(function (sample) {
            if (sample.old !== undefined) {
                var row = el("div", "cwyc-bulk-sample");
                if (sample.label) {
                    row.appendChild(el("div", "cwyc-bulk-sample-label", sample.label));
                }
                var diff = el("div", "cwyc-field-diff");
                renderDiff(diff, sample.old, sample.new);
                row.appendChild(diff);
                body.appendChild(row);
            } else if (sample.text) {
                body.appendChild(el("div", "cwyc-bulk-sample-text", sample.text));
            }
        });
    }

    function renderSkillUpdateBody(record, body) {
        // Pattern statements the user judges, then the skill diff to audit.
        var data = record.data;
        if ((data.samples || []).length) {
            var list = el("ul", "cwyc-skill-patterns");
            data.samples.forEach(function (sample) {
                if (sample.text) {
                    list.appendChild(el("li", "", sample.text));
                }
            });
            body.appendChild(list);
        }
        if (data.count) {
            body.appendChild(
                el("div", "cwyc-skill-basis",
                    "Based on " + data.count + " observation(s) of your edits. " +
                    "Accepting updates the card-authoring skill and clears them; " +
                    "the previous version is archived.")
            );
        }
        var diff = (data.op_args || {}).diff || "";
        if (diff) {
            var details = document.createElement("details");
            details.className = "cwyc-cs-details";
            var summary = document.createElement("summary");
            summary.textContent = "Skill diff";
            details.appendChild(summary);
            var pre = document.createElement("pre");
            pre.className = "cwyc-skill-diff";
            pre.textContent = diff;
            details.appendChild(pre);
            body.appendChild(details);
        }
    }

    function renderChangeSetBody(record, body) {
        var data = record.data;
        if (data.open) {
            body.appendChild(
                el("div", "cwyc-cs-collecting",
                    "Collecting edits… " + data.count + " note(s) so far")
            );
            return;
        }
        body.appendChild(
            el("div", "cwyc-bulk-op", data.count + " note(s) in this change set")
        );
        (data.samples || []).forEach(function (sample) {
            var row = el("div", "cwyc-bulk-sample");
            row.appendChild(el("div", "cwyc-bulk-sample-label", sample.label));
            var diff = el("div", "cwyc-field-diff");
            renderDiff(diff, sample.old || "", sample.new || "");
            row.appendChild(diff);
            body.appendChild(row);
        });
        var items = data.items || [];
        if (items.length > (data.samples || []).length) {
            var details = document.createElement("details");
            details.className = "cwyc-cs-details";
            var summary = document.createElement("summary");
            summary.textContent = "All " + items.length + " notes";
            details.appendChild(summary);
            var list = el("div", "cwyc-cs-list");
            items.forEach(function (item) {
                list.appendChild(
                    el("div", "cwyc-cs-item",
                        (item.label || item.note_id) + " — " +
                        (item.fields || []).join(", "))
                );
            });
            details.appendChild(list);
            body.appendChild(details);
        }
    }

    function autosizeArea(area) {
        area.style.height = "auto";
        area.style.height = Math.min(area.scrollHeight, 200) + "px";
    }

    // Reusable tag-chip editor (proposal cards). Autocompletes from the same
    // shared #cwyc-tag-options datalist the pins panel populates.
    function createTagEditor(initialTags) {
        var container = el("div", "cwyc-tag-input");
        var entry = document.createElement("input");
        entry.type = "text";
        entry.className = "cwyc-tag-entry";
        entry.placeholder = "add a tag…";
        entry.autocomplete = "off";
        entry.setAttribute("list", "cwyc-tag-options");
        container.appendChild(entry);
        var tags = (initialTags || []).slice();

        function render() {
            container.querySelectorAll(".cwyc-tag-chip").forEach(function (c) {
                c.remove();
            });
            tags.forEach(function (tag) {
                var chip = el("span", "cwyc-tag-chip");
                chip.appendChild(document.createTextNode(tag));
                var x = document.createElement("button");
                x.type = "button";
                x.className = "cwyc-tag-chip-x";
                x.textContent = "×";
                x.title = "Remove " + tag;
                x.addEventListener("click", function () {
                    tags = tags.filter(function (t) { return t !== tag; });
                    render();
                });
                chip.appendChild(x);
                container.insertBefore(chip, entry);
            });
        }
        function add(raw) {
            (raw || "").split(/[\s,]+/).forEach(function (part) {
                var t = part.trim();
                if (t && tags.indexOf(t) === -1) {
                    tags.push(t);
                }
            });
            render();
        }
        entry.addEventListener("keydown", function (event) {
            if (event.key === " " || event.key === "Enter" || event.key === ",") {
                event.preventDefault();
                if (entry.value.trim()) { add(entry.value); entry.value = ""; }
            } else if (event.key === "Backspace" && !entry.value && tags.length) {
                event.preventDefault();
                tags.pop();
                render();
            }
        });
        entry.addEventListener("blur", function () {
            if (entry.value.trim()) { add(entry.value); entry.value = ""; }
        });
        container.addEventListener("click", function () { entry.focus(); });
        render();
        return {
            el: container,
            get: function () {
                if (entry.value.trim()) { add(entry.value); entry.value = ""; }
                return tags.slice();
            },
        };
    }

    function fillDeckSelect(select, values, current) {
        select.innerHTML = "";
        var all = values.slice();
        if (current && all.indexOf(current) === -1) {
            all.unshift(current); // keep the proposed deck even if new
        }
        all.forEach(function (value) {
            var opt = document.createElement("option");
            opt.value = value;
            opt.textContent = value;
            select.appendChild(opt);
        });
        select.value = current || all[0] || "";
    }

    function renderProposal(data) {
        breakAssistantBlock();
        var existing = proposals[data.id];
        var record = existing || {
            data: data,
            root: null,
            fieldInputs: {},
            included: {},
        };
        record.data = data;
        record.fieldInputs = {};

        var root = el("div", "cwyc-proposal");
        root.tabIndex = 0;
        root.dataset.proposalId = data.id;

        // header: kind badge, note type -> deck, status pill
        var head = el("div", "cwyc-proposal-head");
        var badge = el("span", "cwyc-proposal-kind", KIND_BADGES[data.kind] || data.kind);
        if (data.kind === "delete") {
            badge.classList.add("cwyc-kind-delete");
        }
        head.appendChild(badge);
        // Creates carry an editable deck below; edits show their deck here.
        var where = "";
        if (data.kind === "create" || data.kind === "edit") {
            where = data.note_type +
                (data.kind === "edit" && data.deck ? " · " + data.deck : "");
        } else if (data.kind === "change_set") {
            where = data.title || "";
        } else if (data.kind === "deck_op") {
            where = data.deck || "";
        } else if (data.kind === "skill_update") {
            where = data.title || "Card-authoring skill";
        }
        head.appendChild(el("span", "cwyc-proposal-where", where));
        var pill = el("span", "cwyc-proposal-status", proposalStatusLabel(data.status));
        pill.classList.add("cwyc-status-" + data.status);
        head.appendChild(pill);
        root.appendChild(head);
        record.pill = pill;

        (data.warnings || []).forEach(function (warning) {
            root.appendChild(el("div", "cwyc-proposal-warning", warning));
        });
        if (data.rationale) {
            root.appendChild(el("div", "cwyc-proposal-rationale", data.rationale));
        }

        var body = el("div", "cwyc-proposal-body");
        root.appendChild(body);

        if (data.kind === "edit") {
            renderEditFields(record, body);
        } else if (data.kind === "create") {
            renderCreateFields(record, body);
        } else if (data.kind === "change_set") {
            renderChangeSetBody(record, body);
        } else if (data.kind === "skill_update") {
            renderSkillUpdateBody(record, body);
        } else {
            renderBulkBody(record, body); // bulk + delete
        }

        if (data.kind === "create") {
            // Editable destination: deck it lands in, and its tags.
            var deckRow = el("div", "cwyc-proposal-field-row");
            deckRow.appendChild(el("span", "cwyc-proposal-field-label", "Deck"));
            var deckSelect = document.createElement("select");
            deckSelect.className = "cwyc-proposal-deck";
            fillDeckSelect(deckSelect, collectionMeta.decks, data.deck);
            deckRow.appendChild(deckSelect);
            body.appendChild(deckRow);
            record.deckSelect = deckSelect;

            var tagsRow = el("div", "cwyc-proposal-field-row");
            tagsRow.appendChild(el("span", "cwyc-proposal-field-label", "Tags"));
            var tagEditor = createTagEditor(data.tags || []);
            tagsRow.appendChild(tagEditor.el);
            body.appendChild(tagsRow);
            record.tagEditor = tagEditor;
        } else {
            // Edit: read-only current tags plus proposed add/remove.
            var tagRow = el("div", "cwyc-proposal-tags");
            (data.tags || []).forEach(function (tag) {
                tagRow.appendChild(el("span", "cwyc-tag", tag));
            });
            (data.add_tags || []).forEach(function (tag) {
                tagRow.appendChild(el("span", "cwyc-tag cwyc-tag-add", "+ " + tag));
            });
            (data.remove_tags || []).forEach(function (tag) {
                tagRow.appendChild(el("span", "cwyc-tag cwyc-tag-remove", "− " + tag));
            });
            if (tagRow.childNodes.length) {
                body.appendChild(tagRow);
            }
        }

        record.previewBody = body;
        record.previewActive = record.previewActive || 1;
        if (data.previews) {
            var preview = buildPreviewTabs(data.previews, record.previewActive, function (i) {
                record.previewActive = i;
            });
            if (preview) {
                body.appendChild(preview);
                record.previewWrap = preview;
            }
        }

        var errorBox = el("div", "cwyc-proposal-error");
        errorBox.hidden = true;
        root.appendChild(errorBox);
        record.errorBox = errorBox;

        var actions = el("div", "cwyc-proposal-actions");
        if (data.status === "pending" && !data.open) {
            var suggest = el("button", "cwyc-btn-suggest", "Suggest change");
            suggest.type = "button";
            suggest.title = "Tell the assistant how to change this card";
            suggest.addEventListener("click", function () {
                suggestChange(record);
            });
            var reject = el("button", "cwyc-btn-reject", "Reject");
            reject.type = "button";
            reject.title = "Reject (Cmd/Ctrl+Backspace)";
            reject.addEventListener("click", function () {
                post({ type: "proposal_reject", id: data.id });
            });
            var acceptLabel = data.kind === "delete" ? "Delete"
                : data.kind === "skill_update" ? "Update skill"
                : data.kind === "deck_op" ? "Apply" : "Accept";
            var acceptCls = "cwyc-btn-accept cwyc-primary" +
                (data.kind === "delete" ? " cwyc-btn-danger" : "");
            var accept = el("button", acceptCls, acceptLabel);
            accept.type = "button";
            accept.title = data.kind === "delete"
                ? "Permanently delete these notes"
                : "Accept (Cmd/Ctrl+Enter)";
            accept.addEventListener("click", function () {
                acceptProposal(data.id);
            });
            actions.appendChild(suggest);
            actions.appendChild(reject);
            actions.appendChild(accept);
        }
        root.appendChild(actions);
        record.actions = actions;

        root.addEventListener("focusin", function () {
            activeProposalId = data.id;
            root.classList.add("cwyc-proposal-active");
        });
        root.addEventListener("focusout", function () {
            root.classList.remove("cwyc-proposal-active");
        });

        if (existing && existing.root) {
            existing.root.replaceWith(root);
        } else {
            var row = el("div", "cwyc-row cwyc-row-proposal");
            row.appendChild(root);
            transcript.appendChild(row);
            proposalOrder.push(data.id);
        }
        record.root = root;
        proposals[data.id] = record;
        if (data.status === "pending" && !activeProposalId) {
            activeProposalId = data.id;
        }
        if (data.status !== "pending") {
            markResolved(data.id, data.status, {});
        }
        updateBulkBar();
        scrollToBottomIfPinned();
    }

    function updateProposalPreview(id, previews) {
        var record = proposals[id];
        if (!record || !previews || record.data.status !== "pending") {
            return;
        }
        var fresh = buildPreviewTabs(previews, record.previewActive, function (i) {
            record.previewActive = i;
        });
        if (!fresh) {
            return;
        }
        if (record.previewWrap && record.previewWrap.parentNode) {
            record.previewWrap.replaceWith(fresh);
        } else if (record.previewBody) {
            record.previewBody.appendChild(fresh);
        }
        record.previewWrap = fresh;
    }

    var previewTimers = {};

    function schedulePreview(record) {
        // Debounced live re-render of the card as the user edits fields.
        if (!record || record.data.status !== "pending") {
            return;
        }
        var id = record.data.id;
        clearTimeout(previewTimers[id]);
        previewTimers[id] = setTimeout(function () {
            var fields = {};
            Object.keys(record.fieldInputs).forEach(function (name) {
                fields[name] = record.fieldInputs[name].value;
            });
            post({ type: "proposal_preview", id: id, fields: fields });
        }, 400);
    }

    function plainSnippet(html, limit) {
        var div = document.createElement("div");
        div.innerHTML = html || "";
        var text = (div.textContent || "").replace(/\s+/g, " ").trim();
        limit = limit || 50;
        return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
    }

    function suggestChange(record) {
        var d = record.data;
        var first = d.fields && d.fields[0] ? plainSnippet(d.fields[0].new) : "";
        var ref =
            d.kind === "edit"
                ? "the edit to note " + d.note_id
                : "the proposed " + d.note_type + " card";
        if (first) {
            ref += ' ("' + first + '")';
        }
        revisingProposalId = d.id; // set aside on send (supersedeRevisingProposal)
        input.value = "For " + ref + ", please ";
        input.focus();
        autosizeInput();
        input.selectionStart = input.selectionEnd = input.value.length;
    }

    function fieldEditor(record, name, value) {
        var area = document.createElement("textarea");
        area.className = "cwyc-field-input";
        area.value = value;
        area.rows = 1;
        area.addEventListener("input", function () {
            autosizeArea(area);
            schedulePreview(record);
        });
        record.fieldInputs[name] = area;
        // Autosize once attached to the DOM (scrollHeight is 0 before).
        setTimeout(function () {
            autosizeArea(area);
        }, 0);
        return area;
    }

    function renderCreateFields(record, body) {
        record.data.fields.forEach(function (fieldData) {
            var row = el("div", "cwyc-field");
            row.appendChild(el("label", "cwyc-field-name", fieldData.name));
            row.appendChild(fieldEditor(record, fieldData.name, fieldData.new));
            body.appendChild(row);
        });
    }

    function renderEditFields(record, body) {
        record.data.fields.forEach(function (fieldData) {
            record.included[fieldData.name] = record.included[fieldData.name] !== false;

            var row = el("div", "cwyc-field");
            var head = el("div", "cwyc-field-head");
            var toggle = document.createElement("input");
            toggle.type = "checkbox";
            toggle.checked = record.included[fieldData.name];
            toggle.title = "Apply this field change";
            toggle.addEventListener("change", function () {
                record.included[fieldData.name] = toggle.checked;
                row.classList.toggle("cwyc-field-skipped", !toggle.checked);
            });
            head.appendChild(toggle);
            head.appendChild(el("label", "cwyc-field-name", fieldData.name));

            var editBtn = el("button", "cwyc-field-edit-btn", "Edit");
            editBtn.type = "button";
            editBtn.title = "Edit the proposed value";
            head.appendChild(editBtn);
            row.appendChild(head);

            var diffBox = el("div", "cwyc-field-diff");
            renderDiff(diffBox, fieldData.old || "", fieldData.new);
            row.appendChild(diffBox);

            var area = fieldEditor(record, fieldData.name, fieldData.new);
            area.hidden = true;
            row.appendChild(area);
            editBtn.addEventListener("click", function () {
                var editing = area.hidden;
                area.hidden = !editing;
                diffBox.hidden = editing;
                editBtn.textContent = editing ? "Diff" : "Edit";
                if (editing) {
                    autosizeArea(area);
                    area.focus();
                } else {
                    renderDiff(diffBox, fieldData.old || "", area.value);
                }
            });
            body.appendChild(row);
        });
    }

    function acceptProposal(id) {
        var record = proposals[id];
        if (!record || record.data.status !== "pending") {
            return;
        }
        var fields = {};
        Object.keys(record.fieldInputs).forEach(function (name) {
            fields[name] = record.fieldInputs[name].value;
        });
        var msg = { type: "proposal_accept", id: id, fields: fields };
        if (record.data.kind === "edit") {
            msg.accepted_fields = Object.keys(fields).filter(function (name) {
                return record.included[name] !== false;
            });
        } else {
            if (record.deckSelect) {
                msg.deck = record.deckSelect.value;
            }
            if (record.tagEditor) {
                msg.tags = record.tagEditor.get();
            }
        }
        post(msg);
    }

    function rejectProposal(id) {
        var record = proposals[id];
        if (!record || record.data.status !== "pending") {
            return;
        }
        post({ type: "proposal_reject", id: id });
    }

    function addActionButton(record, label, cls, title, handler) {
        var button = el("button", cls, label);
        button.type = "button";
        button.title = title;
        button.addEventListener("click", handler);
        record.actions.appendChild(button);
    }

    function markResolved(id, status, extra) {
        var record = proposals[id];
        if (!record) {
            return;
        }
        extra = extra || {};
        record.data.status = status;
        record.root.classList.add("cwyc-proposal-resolved");
        record.root.classList.toggle("cwyc-proposal-superseded", status === "superseded");
        record.pill.textContent = proposalStatusLabel(status);
        record.pill.className = "cwyc-proposal-status cwyc-status-" + status;
        record.root
            .querySelectorAll("textarea, input, .cwyc-field-edit-btn")
            .forEach(function (node) {
                node.disabled = true;
            });
        // Post-apply findings (stats drift, skipped-stale notes) surface on
        // the card itself.
        (extra.warnings || []).forEach(function (warning) {
            record.root.insertBefore(
                el("div", "cwyc-proposal-warning", warning),
                record.actions
            );
        });
        record.actions.innerHTML = "";
        var revertible = extra.revertible !== false;
        if ((status === "accepted" || status === "auto-accepted") && revertible) {
            addActionButton(record, "Revert", "cwyc-btn-revert", "Undo this change", function () {
                post({ type: "proposal_revert", id: id });
            });
        } else if (
            status === "undone" &&
            (record.data.kind === "create" || record.data.kind === "edit")
        ) {
            addActionButton(record, "Re-add", "cwyc-btn-readd", "Add this note again", function () {
                post({ type: "proposal_readd", id: id });
            });
        } else if (status === "superseded" || status === "rejected") {
            addActionButton(
                record,
                "Restore",
                "cwyc-btn-restore",
                "Bring this proposal back for review",
                function () {
                    post({ type: "proposal_restore", id: id });
                }
            );
        }
        if (activeProposalId === id) {
            activeProposalId = firstPendingProposal();
        }
        updateBulkBar();
    }

    function pendingProposalIds() {
        return proposalOrder.filter(function (id) {
            return (
                proposals[id] &&
                proposals[id].data.status === "pending" &&
                !proposals[id].data.open
            );
        });
    }

    function updateBulkBar() {
        var bar = document.getElementById("cwyc-bulk");
        if (!bar) {
            return;
        }
        var pending = pendingProposalIds();
        bar.hidden = pending.length < 2;
        var label = document.getElementById("cwyc-bulk-label");
        if (label) {
            label.textContent = pending.length + " proposals pending";
        }
    }

    function firstPendingProposal() {
        for (var i = 0; i < proposalOrder.length; i++) {
            var record = proposals[proposalOrder[i]];
            if (record && record.data.status === "pending") {
                return proposalOrder[i];
            }
        }
        return null;
    }

    function targetProposal() {
        var record = activeProposalId && proposals[activeProposalId];
        if (record && record.data.status === "pending") {
            return activeProposalId;
        }
        return firstPendingProposal();
    }

    function cycleProposalFocus(direction) {
        var pending = proposalOrder.filter(function (id) {
            return proposals[id] && proposals[id].data.status === "pending";
        });
        if (!pending.length) {
            return;
        }
        var index = pending.indexOf(activeProposalId);
        index = index === -1 ? 0 : (index + direction + pending.length) % pending.length;
        activeProposalId = pending[index];
        var root = proposals[activeProposalId].root;
        root.focus();
        root.scrollIntoView({ block: "nearest" });
    }

    /* ---- ledger strip ---- */

    var ledgerBox = null;

    function renderLedger(data) {
        if (!ledgerBox) {
            return;
        }
        var live = (data.entries || []).filter(function (entry) {
            return !entry.undone;
        });
        ledgerBox.hidden = live.length === 0;
        var label = document.getElementById("cwyc-ledger-label");
        if (label) {
            label.textContent =
                live.length + " AI change" + (live.length === 1 ? "" : "s") + " this session";
        }
    }

    /* ---- pins panel ---- */

    function fillSelect(select, values, current) {
        while (select.options.length > 1) {
            select.remove(1); // keep the leading "— none —" option
        }
        values.forEach(function (value) {
            var opt = document.createElement("option");
            opt.value = value;
            opt.textContent = value;
            select.appendChild(opt);
        });
        // A pinned value that no longer exists is still shown, not dropped.
        if (current && values.indexOf(current) === -1) {
            var ghost = document.createElement("option");
            ghost.value = current;
            ghost.textContent = current + " (missing)";
            select.appendChild(ghost);
        }
        select.value = current || "";
    }

    function noteTypeNames() {
        return collectionMeta.note_types.map(function (nt) { return nt.name; });
    }

    function renderCollectionMeta(data) {
        collectionMeta = {
            decks: data.decks || [],
            note_types: data.note_types || [],
            tags: data.tags || [],
        };
        fillSelect(document.getElementById("cwyc-pin-deck"), collectionMeta.decks, pins.deck);
        fillSelect(
            document.getElementById("cwyc-pin-notetype"),
            noteTypeNames(),
            pins.note_type
        );
        var datalist = document.getElementById("cwyc-tag-options");
        datalist.innerHTML = "";
        collectionMeta.tags.forEach(function (tag) {
            var opt = document.createElement("option");
            opt.value = tag;
            datalist.appendChild(opt);
        });
        renderPinFields();
    }

    function fieldsForNoteType(name) {
        var nt = collectionMeta.note_types.filter(function (t) {
            return t.name === name;
        })[0];
        return nt ? nt.fields : [];
    }

    function renderPinFields() {
        var container = document.getElementById("cwyc-pin-fields");
        var empty = document.getElementById("cwyc-pin-fields-empty");
        var noteType = document.getElementById("cwyc-pin-notetype").value;
        var fields = fieldsForNoteType(noteType);
        container.innerHTML = "";
        if (!noteType || !fields.length) {
            empty.hidden = false;
            return;
        }
        empty.hidden = true;
        fields.forEach(function (name) {
            var row = el("div", "cwyc-pin-field");
            row.appendChild(el("label", "cwyc-pin-field-name", name));
            var input = document.createElement("input");
            input.type = "text";
            input.className = "cwyc-pin-field-input";
            input.dataset.field = name;
            input.value = (pins.fields || {})[name] || "";
            input.placeholder = "default (optional)";
            input.addEventListener("input", debouncedSavePins);
            row.appendChild(input);
            container.appendChild(row);
        });
    }

    function renderPins(data) {
        pins = data || { deck: "", note_type: "", tags: [], fields: {} };
        var label = document.getElementById("cwyc-pins-label");
        var count =
            (pins.deck ? 1 : 0) +
            (pins.note_type ? 1 : 0) +
            ((pins.tags || []).length ? 1 : 0) +
            (Object.keys(pins.fields || {}).length ? 1 : 0);
        label.textContent = count ? "Pins · " + count : "Pins";
        fillSelect(document.getElementById("cwyc-pin-deck"), collectionMeta.decks, pins.deck);
        fillSelect(
            document.getElementById("cwyc-pin-notetype"),
            noteTypeNames(),
            pins.note_type
        );
        setPinTags(pins.tags || []);
        renderPinFields();
    }

    /* ---- tag chip editor (Anki-editor-like: space/enter commits a tag) ---- */

    function renderTagChips() {
        var container = document.getElementById("cwyc-pin-tags");
        var entry = document.getElementById("cwyc-pin-tag-entry");
        container.querySelectorAll(".cwyc-tag-chip").forEach(function (chip) {
            chip.remove();
        });
        pinTags.forEach(function (tag) {
            var chip = el("span", "cwyc-tag-chip");
            chip.appendChild(document.createTextNode(tag));
            var remove = document.createElement("button");
            remove.type = "button";
            remove.className = "cwyc-tag-chip-x";
            remove.textContent = "×";
            remove.title = "Remove " + tag;
            remove.addEventListener("click", function () {
                removePinTag(tag);
            });
            chip.appendChild(remove);
            container.insertBefore(chip, entry);
        });
    }

    function addPinTagValue(raw) {
        // Anki tags are whitespace-separated; split defensively so a pasted
        // "a b, c" becomes three chips.
        (raw || "").split(/[\s,]+/).forEach(function (part) {
            var tag = part.trim();
            if (tag && pinTags.indexOf(tag) === -1) {
                pinTags.push(tag);
            }
        });
    }

    function setPinTags(tags) {
        pinTags = [];
        (tags || []).forEach(addPinTagValue);
        renderTagChips();
    }

    function removePinTag(tag) {
        pinTags = pinTags.filter(function (t) {
            return t !== tag;
        });
        renderTagChips();
        savePins();
    }

    function commitTagEntry() {
        var entry = document.getElementById("cwyc-pin-tag-entry");
        if (entry.value.trim()) {
            addPinTagValue(entry.value);
            entry.value = "";
            renderTagChips();
            savePins();
        }
    }

    // Pins persist automatically on any change — no explicit Save.
    function savePins() {
        post({ type: "set_pins", pins: collectPins() });
    }

    var pinsSaveTimer = null;
    function debouncedSavePins() {
        clearTimeout(pinsSaveTimer);
        pinsSaveTimer = setTimeout(savePins, 400);
    }

    /* ---- agent (model / effort) ---- */

    var MODEL_LABELS = {
        "": "Default",
        fable: "Fable",
        opus: "Opus",
        sonnet: "Sonnet",
        haiku: "Haiku",
    };

    function renderAgent(data) {
        var model = data.model || "";
        var effort = data.effort || "";
        var modelLabel = MODEL_LABELS[model] || model || "Default";
        var label = document.getElementById("cwyc-agent-label");
        label.textContent = effort ? modelLabel + " · " + effort : modelLabel;
        document.getElementById("cwyc-agent-model").value = model;
        document.getElementById("cwyc-agent-effort").value = effort;
        if (data.mode) {
            renderMode(data.mode);
        }
    }

    function sendAgent() {
        post({
            type: "set_agent",
            model: document.getElementById("cwyc-agent-model").value,
            effort: document.getElementById("cwyc-agent-effort").value,
        });
    }

    /* ---- permission mode chip ---- */

    // Ordered most-oversight → most-autonomy (also the Shift+Tab cycle order):
    // approve everything → no writes → review writes → auto-create → write freely.
    var MODES = ["ask-each-read", "read-only", "default", "auto-accept", "trusted-writes"];
    var MODE_LABELS = {
        "default": "Propose",
        "ask-each-read": "Ask reads",
        "read-only": "Read-only",
        "auto-accept": "Auto-accept",
        "trusted-writes": "Trusted",
    };
    var MODE_HINTS = {
        "ask-each-read": "You approve every read of your collection; writes still proposed.",
        "read-only": "The assistant can look but never write.",
        "default": "Reads are free; writes come to you as proposals to review.",
        "auto-accept": "New notes apply automatically (capped); edits and deletes still ask.",
        "trusted-writes": "Every write applies directly; only deletes ask.",
    };
    var currentMode = "default";

    var TRUSTED_CONFIRM =
        "Trusted-writes lets the assistant change your collection directly " +
        "(deletes still ask). Everything is ledgered and revertible, with a " +
        "per-session write budget. Enable?";

    function renderMode(mode) {
        currentMode = MODES.indexOf(mode) === -1 ? "default" : mode;
        var chip = document.getElementById("cwyc-mode-chip");
        var label = document.getElementById("cwyc-mode-label");
        if (label) {
            label.textContent = MODE_LABELS[currentMode];
        }
        if (chip) {
            // Preserve the caret child; only swap the modifier class.
            chip.className = "cwyc-mode-" + currentMode;
        }
        highlightModeMenu();
    }

    function buildModeMenu() {
        var menu = document.getElementById("cwyc-mode-menu");
        if (!menu) {
            return;
        }
        menu.innerHTML = "";
        MODES.forEach(function (mode) {
            var item = el("button", "cwyc-menu-item");
            item.type = "button";
            item.dataset.mode = mode;
            item.appendChild(el("span", "cwyc-menu-item-label", MODE_LABELS[mode]));
            item.appendChild(el("span", "cwyc-menu-item-hint", MODE_HINTS[mode] || ""));
            item.addEventListener("click", function () {
                menu.hidden = true;
                selectMode(mode);
            });
            menu.appendChild(item);
        });
        highlightModeMenu();
    }

    function highlightModeMenu() {
        var menu = document.getElementById("cwyc-mode-menu");
        if (!menu) {
            return;
        }
        menu.querySelectorAll(".cwyc-menu-item").forEach(function (item) {
            item.classList.toggle(
                "cwyc-menu-item-active",
                item.dataset.mode === currentMode
            );
        });
    }

    function selectMode(mode) {
        // Direct pick from the menu. Trusted-writes needs an explicit yes;
        // declining just cancels (unlike Shift+Tab cycling, which skips past).
        if (mode === currentMode) {
            return;
        }
        if (mode === "trusted-writes" && !window.confirm(TRUSTED_CONFIRM)) {
            return;
        }
        post({ type: "set_permission_mode", mode: mode });
    }

    function cycleMode() {
        var next = MODES[(MODES.indexOf(currentMode) + 1) % MODES.length];
        if (next === "trusted-writes" && !window.confirm(TRUSTED_CONFIRM)) {
            next = MODES[(MODES.indexOf(next) + 1) % MODES.length];
        }
        post({ type: "set_permission_mode", mode: next });
    }

    /* ---- open-in-Claude-Code split button ---- */

    var OPEN_TARGET_LABELS = { terminal: "Terminal", gui: "Desktop" };
    var openTarget = "terminal";

    function renderOpenTarget() {
        var label = document.getElementById("cwyc-open-cc-label");
        if (label) {
            label.textContent = OPEN_TARGET_LABELS[openTarget] || "Terminal";
        }
        var menu = document.getElementById("cwyc-open-cc-menu");
        if (menu) {
            menu.querySelectorAll(".cwyc-menu-item").forEach(function (item) {
                item.classList.toggle(
                    "cwyc-menu-item-active", item.dataset.target === openTarget
                );
            });
        }
    }

    function setOpenTarget(target) {
        // Picking from the dropdown only changes the default; it does not open.
        if (target !== "terminal" && target !== "gui") {
            return;
        }
        openTarget = target;
        renderOpenTarget();
        post({ type: "set_open_in_claude_target", target: target });
    }

    /* ---- usage chip ---- */

    function renderUsage(payload) {
        var chip = document.getElementById("cwyc-usage-chip");
        if (!chip) {
            return;
        }
        var parts = [];
        if (payload.cost_usd !== null && payload.cost_usd !== undefined) {
            parts.push("$" + payload.cost_usd.toFixed(payload.cost_usd < 1 ? 3 : 2));
        }
        var tokens = (payload.input_tokens || 0) + (payload.output_tokens || 0);
        if (tokens) {
            parts.push(
                tokens >= 1000 ? Math.round(tokens / 1000) + "k tokens" : tokens + " tokens"
            );
        }
        chip.textContent = parts.join(" · ");
        chip.hidden = parts.length === 0;
    }

    /* ---- suggested questions (ghost text via placeholder) ---- */

    var uiConfig = { suggested_questions: true };
    var contextKind = "overview";
    var suggestionIndex = 0;
    var currentSuggestion = "";
    var DEFAULT_PLACEHOLDER = "Ask about this card…";

    var SUGGESTIONS = {
        card: [
            "Explain this card to me — I forgot.",
            "Why is this the answer?",
            "Find cards related to this one.",
            "Make a card for the part of this I keep missing.",
        ],
        overview: [
            "What should I study today?",
            "Which decks are getting rusty?",
            "What did I create with you this week?",
        ],
    };

    function refreshSuggestion() {
        if (!input) {
            return;
        }
        if (!uiConfig.suggested_questions) {
            currentSuggestion = "";
            input.placeholder = DEFAULT_PLACEHOLDER;
            return;
        }
        var pool = SUGGESTIONS[contextKind] || SUGGESTIONS.overview;
        currentSuggestion = pool[suggestionIndex % pool.length];
        input.placeholder = currentSuggestion + "  (Tab)";
    }

    function acceptSuggestion() {
        if (!currentSuggestion || input.value) {
            return false;
        }
        input.value = currentSuggestion;
        suggestionIndex += 1; // rotate for next time
        autosizeInput();
        input.setSelectionRange(input.value.length, input.value.length);
        return true;
    }

    /* ---- ask-each-read approval chips ---- */

    var approvalChips = {}; // id -> element

    function renderApprovalRequest(payload) {
        breakAssistantBlock();
        var chip = el("div", "cwyc-approval");
        var label = friendlyTool(payload.tool) || payload.tool;
        chip.appendChild(el("span", "cwyc-approval-label", label));
        if (payload.summary) {
            chip.appendChild(el("span", "cwyc-approval-summary", toolHint(payload.summary)));
        }
        var deny = el("button", "cwyc-approval-deny", "Deny");
        deny.type = "button";
        var allow = el("button", "cwyc-approval-allow cwyc-primary", "Allow");
        allow.type = "button";
        function answer(ok) {
            post({ type: "tool_approval_response", id: payload.id, allow: ok });
        }
        deny.addEventListener("click", function () { answer(false); });
        allow.addEventListener("click", function () { answer(true); });
        chip.appendChild(deny);
        chip.appendChild(allow);
        var row = el("div", "cwyc-row cwyc-row-tool");
        row.appendChild(chip);
        transcript.appendChild(row);
        approvalChips[payload.id] = chip;
        scrollToBottomIfPinned();
    }

    function resolveApproval(payload) {
        var chip = approvalChips[payload.id];
        if (!chip) {
            return;
        }
        chip.querySelectorAll("button").forEach(function (b) { b.remove(); });
        chip.classList.add(payload.allow ? "cwyc-approval-ok" : "cwyc-approval-no");
        chip.appendChild(
            el("span", "cwyc-approval-result",
                payload.allow ? "allowed" : payload.reason === "timed out" ? "timed out" : "denied")
        );
        delete approvalChips[payload.id];
    }

    /* ---- doctor panel ---- */

    function renderDoctor(results) {
        var box = document.getElementById("cwyc-doctor-results");
        box.innerHTML = "";
        (results || []).forEach(function (row) {
            var item = el("div", "cwyc-doctor-row cwyc-doctor-" + row.status);
            item.appendChild(el("span", "cwyc-doctor-status",
                row.status === "ok" ? "✓" : row.status === "warn" ? "•" : "✗"));
            var body = el("div", "cwyc-doctor-body");
            body.appendChild(el("div", "cwyc-doctor-label", row.label));
            body.appendChild(el("div", "cwyc-doctor-detail", row.detail || ""));
            if (row.link && row.status !== "ok") {
                var a = document.createElement("a");
                a.href = row.link;
                a.textContent = "install page";
                a.className = "cwyc-doctor-link";
                body.appendChild(a);
            }
            item.appendChild(body);
            box.appendChild(item);
        });
    }

    /* ---- chat history ---- */

    function renderHistory(sessions) {
        var list = document.getElementById("cwyc-history-list");
        var empty = document.getElementById("cwyc-history-empty");
        list.innerHTML = "";
        empty.hidden = (sessions || []).length > 0;
        (sessions || []).forEach(function (meta) {
            var item = el("button", "cwyc-history-item");
            item.type = "button";
            item.appendChild(el("div", "cwyc-history-title", meta.title || "(untitled)"));
            var when = meta.updated_at
                ? new Date(meta.updated_at * 1000).toLocaleString()
                : "";
            item.appendChild(
                el("div", "cwyc-history-sub", when + " · " + (meta.events || 0) + " events")
            );
            item.addEventListener("click", function () {
                post({ type: "load_history", id: meta.id });
                document.getElementById("cwyc-history-panel").hidden = true;
            });
            list.appendChild(item);
        });
    }

    function replayHistory(payload) {
        resetTranscript();
        (payload.events || []).forEach(function (ev) {
            if (ev.type === "user_message") {
                addUserMessage(ev.text);
            } else if (ev.type === "assistant_text") {
                startAssistantMessage();
                appendDelta(ev.text);
                breakAssistantBlock();
            } else {
                dispatch(ev);
            }
        });
        // Proposals left pending in the old session can no longer be acted
        // on (their manager state is gone) - set them aside visually.
        pendingProposalIds().forEach(function (id) {
            markResolved(id, "superseded", {});
        });
        setStreaming(false);
        scrollToBottomIfPinned();
    }

    function collectPins() {
        var fields = {};
        document
            .getElementById("cwyc-pin-fields")
            .querySelectorAll(".cwyc-pin-field-input")
            .forEach(function (input) {
                var value = input.value.trim();
                if (value) {
                    fields[input.dataset.field] = value;
                }
            });
        return {
            deck: document.getElementById("cwyc-pin-deck").value.trim(),
            note_type: document.getElementById("cwyc-pin-notetype").value.trim(),
            tags: pinTags.slice(),
            fields: fields,
        };
    }

    /* ---- dispatch ---- */

    function dispatch(payload) {
        switch (payload.type) {
            case "text_delta":
                appendDelta(payload.text);
                break;
            case "tool_call_started":
                addToolChip(payload.call_id, payload.tool, payload.summary);
                break;
            case "tool_call_finished":
                finishToolChip(payload.call_id, payload.ok, payload.summary);
                break;
            case "proposal":
                renderProposal(payload.proposal);
                break;
            case "preview_update":
                updateProposalPreview(payload.id, payload.previews);
                break;
            case "proposal_resolved":
                markResolved(payload.id, payload.status, payload);
                break;
            case "proposal_error": {
                var record = proposals[payload.id];
                if (record) {
                    record.errorBox.textContent = payload.message;
                    record.errorBox.hidden = false;
                }
                break;
            }
            case "ledger":
                renderLedger(payload);
                break;
            case "pins":
                renderPins(payload.pins);
                break;
            case "collection_meta":
                renderCollectionMeta(payload);
                break;
            case "agent":
                renderAgent(payload);
                break;
            case "done":
                finalizeStream(false);
                break;
            case "cancelled":
                finalizeStream(true);
                break;
            case "error":
                appendDelta("\n\n**Error:** " + payload.message);
                finalizeStream(false);
                break;
            case "reset":
                resetTranscript();
                break;
            case "notice": {
                var note = el("div", "cwyc-notice", payload.text);
                transcript.appendChild(note);
                scrollToBottomIfPinned();
                break;
            }
            case "context": {
                var chip = document.getElementById("cwyc-context-chip");
                if (chip) {
                    chip.textContent = "Context: " + payload.label;
                }
                if (payload.kind && payload.kind !== contextKind) {
                    contextKind = payload.kind;
                    refreshSuggestion();
                }
                break;
            }
            case "usage":
                renderUsage(payload);
                break;
            case "tool_approval":
                renderApprovalRequest(payload);
                break;
            case "tool_approval_resolved":
                resolveApproval(payload);
                break;
            case "doctor":
                renderDoctor(payload.results);
                break;
            case "history":
                renderHistory(payload.sessions);
                break;
            case "history_load":
                replayHistory(payload);
                break;
            case "ui_config":
                uiConfig.suggested_questions = payload.suggested_questions !== false;
                if (payload.open_in_claude_target) {
                    openTarget =
                        payload.open_in_claude_target === "gui" ? "gui" : "terminal";
                    renderOpenTarget();
                }
                refreshSuggestion();
                break;
            case "learning":
                renderLearning(payload);
                break;
            case "user_message":
                // Python-initiated send (the skill-review kickoff): render the
                // user bubble + streaming state the composer normally handles.
                addUserMessage(payload.text || "");
                startAssistantMessage();
                setStreaming(true);
                break;
            default:
                // Unknown event types are ignored on purpose.
                break;
        }
    }

    /* ---- learning nudge ---- */

    // Dismiss hides the nudge until MORE observations accumulate (not
    // forever): re-nudging on the same count would just nag.
    var learningDismissedAt = -1;
    var learningPending = 0;

    function renderLearning(payload) {
        learningPending = payload.pending || 0;
        var banner = document.getElementById("cwyc-learning-nudge");
        if (!banner) {
            return;
        }
        var show = Boolean(payload.nudge) && learningPending > learningDismissedAt;
        banner.hidden = !show;
        if (show) {
            var text = document.getElementById("cwyc-learning-nudge-text");
            if (text) {
                text.textContent =
                    "You’ve made " + learningPending + " edit(s) to AI-written " +
                    "cards since the last skill review.";
            }
        }
    }

    function focusComposer() {
        // The page is first laid out while the dock is hidden (degenerate
        // viewport), which can leave a stale autosized height behind.
        autosizeInput();
        input.focus();
    }

    function onKeydownCapture(event) {
        var mod = event.metaKey || event.ctrlKey;
        var key = event.key.toLowerCase();
        if (mod && key === "j") {
            event.preventDefault();
            event.stopImmediatePropagation();
            post(event.shiftKey ? { type: "new_chat" } : { type: "toggle_focus" });
            return;
        }
        if (mod && event.key === "Enter") {
            var acceptId = targetProposal();
            if (acceptId) {
                event.preventDefault();
                event.stopImmediatePropagation();
                acceptProposal(acceptId);
            }
            return;
        }
        if (mod && event.key === "Backspace") {
            var rejectId = targetProposal();
            if (rejectId) {
                event.preventDefault();
                event.stopImmediatePropagation();
                rejectProposal(rejectId);
            }
            return;
        }
        if (mod && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            event.stopImmediatePropagation();
            cycleProposalFocus(event.key === "ArrowDown" ? 1 : -1);
            return;
        }
        if (event.key === "Escape") {
            // AnkiWebView injects its own bubble-phase Escape handler that
            // pycmd("close")s; intercept in capture phase and stop it.
            event.preventDefault();
            event.stopImmediatePropagation();
            post(streaming ? { type: "cancel" } : { type: "focus_reviewer" });
        }
    }

    function init() {
        transcript = document.getElementById("cwyc-transcript");
        input = document.getElementById("cwyc-input");
        sendButton = document.getElementById("cwyc-send");
        ledgerBox = document.getElementById("cwyc-ledger");

        sendButton.addEventListener("click", function () {
            if (streaming) {
                post({ type: "cancel" });
            } else {
                sendCurrentInput();
            }
        });
        document.getElementById("cwyc-new-chat").addEventListener("click", function () {
            post({ type: "new_chat" });
        });
        var openMenu = document.getElementById("cwyc-open-cc-menu");
        var modeMenu = document.getElementById("cwyc-mode-menu");
        buildModeMenu();
        document.getElementById("cwyc-mode-chip").addEventListener("click", function (event) {
            event.stopPropagation();
            openMenu.hidden = true;
            modeMenu.hidden = !modeMenu.hidden;
        });
        // Split button: main part opens with the current target; the caret
        // picks/persists it without opening.
        document.getElementById("cwyc-open-cc").addEventListener("click", function () {
            openMenu.hidden = true;
            post({ type: "open_in_claude", target: openTarget });
        });
        document.getElementById("cwyc-open-cc-caret").addEventListener("click", function (event) {
            event.stopPropagation();
            modeMenu.hidden = true;
            openMenu.hidden = !openMenu.hidden;
        });
        openMenu.querySelectorAll(".cwyc-menu-item").forEach(function (item) {
            item.addEventListener("click", function () {
                openMenu.hidden = true;
                setOpenTarget(item.dataset.target);
            });
        });
        renderOpenTarget();
        document.addEventListener("click", function () {
            openMenu.hidden = true;
            modeMenu.hidden = true;
        });
        var doctorPanel = document.getElementById("cwyc-doctor-panel");
        var historyPanel = document.getElementById("cwyc-history-panel");
        document.getElementById("cwyc-doctor-toggle").addEventListener("click", function () {
            doctorPanel.hidden = !doctorPanel.hidden;
            historyPanel.hidden = true;
            if (!doctorPanel.hidden) {
                document.getElementById("cwyc-doctor-results").textContent = "Checking…";
                post({ type: "run_doctor" });
            }
        });
        document.getElementById("cwyc-history-toggle").addEventListener("click", function () {
            historyPanel.hidden = !historyPanel.hidden;
            doctorPanel.hidden = true;
            if (!historyPanel.hidden) {
                post({ type: "list_history" });
            }
        });
        refreshSuggestion();
        input.addEventListener("keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendCurrentInput();
            } else if (event.key === "Tab" && event.shiftKey) {
                // Mirror Claude Code: Shift+Tab cycles the permission mode.
                event.preventDefault();
                cycleMode();
            } else if (event.key === "Tab" && !input.value) {
                if (acceptSuggestion()) {
                    event.preventDefault();
                }
            }
        });
        input.addEventListener("input", autosizeInput);
        window.addEventListener("resize", autosizeInput);
        transcript.addEventListener("scroll", function () {
            var distance =
                transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
            pinnedToBottom = distance < 40;
        });
        document.addEventListener("keydown", onKeydownCapture, true);

        var pinsPanel = document.getElementById("cwyc-pins-panel");
        var agentPanel = document.getElementById("cwyc-agent-panel");
        document.getElementById("cwyc-pins-toggle").addEventListener("click", function () {
            pinsPanel.hidden = !pinsPanel.hidden;
            agentPanel.hidden = true;
        });
        document.getElementById("cwyc-agent-toggle").addEventListener("click", function () {
            agentPanel.hidden = !agentPanel.hidden;
            pinsPanel.hidden = true;
        });
        document.getElementById("cwyc-agent-model").addEventListener("change", sendAgent);
        document.getElementById("cwyc-agent-effort").addEventListener("change", sendAgent);
        document.getElementById("cwyc-pins-clear").addEventListener("click", function () {
            post({ type: "set_pins", pins: { deck: "", note_type: "", tags: [], fields: {} } });
        });
        document.getElementById("cwyc-pin-deck").addEventListener("change", savePins);
        document.getElementById("cwyc-pin-notetype").addEventListener("change", function () {
            renderPinFields();
            savePins();
        });

        var tagEntry = document.getElementById("cwyc-pin-tag-entry");
        tagEntry.addEventListener("keydown", function (event) {
            if (event.key === " " || event.key === "Enter" || event.key === ",") {
                event.preventDefault();
                commitTagEntry();
            } else if (event.key === "Backspace" && !tagEntry.value && pinTags.length) {
                event.preventDefault();
                removePinTag(pinTags[pinTags.length - 1]);
            }
        });
        tagEntry.addEventListener("blur", commitTagEntry);
        document.getElementById("cwyc-pin-tags").addEventListener("click", function () {
            tagEntry.focus();
        });
        document.getElementById("cwyc-learning-review").addEventListener("click", function () {
            document.getElementById("cwyc-learning-nudge").hidden = true;
            post({ type: "start_skill_review" });
        });
        document.getElementById("cwyc-learning-dismiss").addEventListener("click", function () {
            learningDismissedAt = learningPending;
            document.getElementById("cwyc-learning-nudge").hidden = true;
        });
        document.getElementById("cwyc-ledger-undo").addEventListener("click", function () {
            post({ type: "undo_session" });
        });
        document.getElementById("cwyc-ledger-browser").addEventListener("click", function () {
            post({ type: "open_session_browser" });
        });
        document.getElementById("cwyc-bulk-accept").addEventListener("click", function () {
            // Skill updates never ride along a bulk accept: a change to all
            // future agent behavior deserves its own deliberate click.
            pendingProposalIds().forEach(function (id) {
                if (proposals[id].data.kind !== "skill_update") {
                    acceptProposal(id);
                }
            });
        });
        document.getElementById("cwyc-bulk-reject").addEventListener("click", function () {
            pendingProposalIds().forEach(function (id) {
                post({ type: "proposal_reject", id: id });
            });
        });

        autosizeInput();
        pingReadyUntilAcked();
    }

    var readyAcked = false;
    var readyAttempts = 0;

    function pingReadyUntilAcked() {
        // pycmd may not be wired the instant our script runs; retry until
        // Python acknowledges via ackReady() (dispatched on first "ready").
        if (readyAcked || readyAttempts > 40) {
            return;
        }
        readyAttempts += 1;
        post({ type: "ready" });
        setTimeout(pingReadyUntilAcked, 250);
    }

    window.chatUI = {
        dispatch: dispatch,
        focusComposer: focusComposer,
        ackReady: function () {
            readyAcked = true;
        },
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
