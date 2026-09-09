import QtQuick

// Three-line lyrics window (previous / current / next), plus one extra
// holder that only exists mid-transition. LyricsController is deliberately
// pull-based (lineAt/previousLineAt/nextLineAt), not a full line list, so
// only three lines are ever known at once -- but the four physical Text
// holders here are recycled and rotated through roles rather than rebound
// to previous/current/next directly. That's what lets a single piece of
// text keep its identity as it moves between roles (shrinking out of
// "current" into "previous", growing from "next" into "current") and reads
// as one continuous upward scroll instead of the three rows swapping their
// content in place.
//
// Each holder also carries a hidden, non-animated measurement twin sized
// directly at its target font. Two earlier versions of this file used the
// *visible* text's own height for layout math instead, and both broke for
// the same underlying reason: font.pixelSize used to be Behavior-animated,
// so the visible text's height kept changing for the whole 400ms transition
// (word-wrap could even reflow to a different line count partway through).
// Chaining a neighbour's position off that still-mid-flight height either
// meant computing the new layout once from a height that was stale the
// moment it was read (things overlapped), or continuously re-chasing a
// moving number (things looked jerky and still occasionally overlapped).
// The measurement twin has no Behavior, so it reports the correct final
// height the instant role/text change -- which means the whole new layout
// can be computed once, up front, from real end-state numbers, and every
// holder eased cleanly to a fixed target with a single Behavior on y.
//
// font.pixelSize itself no longer animates at all (see the holder
// delegate's `scale` handling below), which also makes the *visible* text's
// height correct the instant role changes -- but the twin stays regardless,
// since it means this layout math never depends on Qt's own text-layout
// timing being synchronous with a property write.
//
// Word-by-word "karaoke" fill (currently active line only): mimics Apple
// Music's lyrics -- upcoming lines dim/grey, sung lines solid/bright, and
// within the line actually playing, each word fills grey-to-bright
// left-to-right as it's sung. Apple's own version reads real word/syllable
// timestamps out of its catalogue's TTML data; LRCLIB (this project's only
// lyrics source, see lrclib.py) only ever gives line-level timestamps, so
// there is no per-word ground truth to read here. What LyricsController's
// currentLineProgress() provides instead is a single 0..1 progress value
// covering the *whole* line's real start/end time, which wordThresholds()
// below divides between the line's words proportionally by character
// count. It tracks the line's actual timing (so a fast- or slow-sung line
// still starts and ends the wipe at the right moments) even though the
// division between individual words within it is an estimate rather than a
// transcript.
Item {
    id: root
    property string previousLine: ""
    property string currentLine: ""
    property string nextLine: ""
    property bool hasLyrics: false
    property real lineProgress: 0

    readonly property real smallSize: 22 * Theme.uiScale
    readonly property real bigSize: 34 * Theme.uiScale
    readonly property real lineGap: Theme.spacingLg
    readonly property int animDuration: Theme.durationSlow

    // Already-sung / not-yet-sung colour pair -- reuses the same two tokens
    // Theme.qml already defines for primary/secondary text (and their
    // over-light-artwork variants, see TrackController.textIsDark), rather
    // than inventing a third pair just for this.
    readonly property color sungColor: app.track.textIsDark ? Theme.colorTextPrimaryOnLight : Theme.colorTextPrimary
    readonly property color pendingColor: app.track.textIsDark ? Theme.colorTextSecondaryOnLight : Theme.colorTextSecondary

    function targetOpacity(role) {
        return role === 2 ? 1.0 : (role === 1 || role === 3 ? 0.5 : 0.0)
    }

    // Splits a line into words and hands back each one's estimated [start,
    // end) fraction of the line's total progress, weighted by character
    // count (plus one "space unit" between each pair of words) -- see this
    // file's header comment for why that's an estimate, not real per-word
    // timing.
    function wordThresholds(text) {
        var parts = text.split(/\s+/).filter(function (w) { return w.length > 0 })
        var result = []
        if (parts.length === 0) return result
        var totalLen = 0
        for (var i = 0; i < parts.length; i++) totalLen += parts[i].length
        totalLen += Math.max(0, parts.length - 1)
        var acc = 0
        for (var j = 0; j < parts.length; j++) {
            var start = acc
            acc += parts[j].length
            var end = acc
            result.push({ word: parts[j], start: start / totalLen, end: end / totalLen })
            acc += 1
        }
        return result
    }

    function holderWithRole(r) {
        for (var i = 0; i < 4; i++) {
            var h = repeater.itemAt(i)
            if (h && h.role === r) return h
        }
        return null
    }

    function computeLayout() {
        var h2 = holderWithRole(2)
        var height2 = h2 ? h2.measuredHeight : 0
        var y2 = viewport.center - height2 / 2

        var h1 = holderWithRole(1)
        var y1 = h1 ? y2 - root.lineGap - h1.measuredHeight : y2

        var y3 = y2 + height2 + root.lineGap

        var h0 = holderWithRole(0)
        var y0 = h0 ? y1 - root.lineGap - h0.measuredHeight : y1

        var targets = {}
        targets[0] = y0
        targets[1] = y1
        targets[2] = y2
        targets[3] = y3
        return targets
    }

    function applyLayout(targets, animate) {
        for (var i = 0; i < 4; i++) {
            var h = repeater.itemAt(i)
            if (!h || targets[h.role] === undefined) continue
            if (!animate) h.animatingEnabled = false
            h.y = targets[h.role]
            if (!animate) h.animatingEnabled = true
        }
    }

    // Re-snaps (no animation) whenever the panel's own geometry changes --
    // window resize, Theme.uiScale changing -- so the layout stays correct
    // between lyric transitions too, not just right after one.
    function resnap() {
        applyLayout(computeLayout(), false)
    }

    onCurrentLineChanged: rotate()

    // rotate() only ever rewrites the text of the one holder it recycles;
    // the other three just change role and keep whatever they were last
    // showing. That's invisible while hasLyrics is false (the whole
    // viewport is hidden), but if currentLine never actually changes value
    // across the gap -- e.g. the new track's lyrics load but its first
    // synced line hasn't started yet, so lineAt() is "" both before and
    // after -- rotate() never runs, and the previous song's stale text is
    // still sitting in those holders the moment the viewport reappears.
    // Wiping all four whenever lyrics become unavailable closes that gap.
    onHasLyricsChanged: if (!hasLyrics) clearHolders()

    function clearHolders() {
        for (var i = 0; i < 4; i++) {
            var h = repeater.itemAt(i)
            if (h) h.text = ""
        }
    }

    function rotate() {
        var items = [repeater.itemAt(0), repeater.itemAt(1), repeater.itemAt(2), repeater.itemAt(3)]
        var recycled = items.find(function (h) { return h.role === 0 })
        var next = items.find(function (h) { return h.role === 3 })

        // In steady state `next` already holds root.currentLine's new value
        // -- it was written here as recycled.text on the *previous* rotate()
        // call, back when it was still the recycled holder taking over the
        // "next" role. This line is a no-op then. It only matters right
        // after a track/lyrics reset: the holder now in the "next" role got
        // its text from Component.onCompleted (or clearHolders()), at a
        // point before this track's lyrics were even loaded, so it's still
        // sitting on "" -- without this, a track's first line never
        // actually gets drawn in any role and playback reads as starting
        // from the second line instead.
        next.text = root.currentLine

        // Snap the recycled holder below the viewport with the freshly
        // revealed next line before it joins the animated shift below --
        // without this it would visibly slide down from "exited" through
        // the visible rows instead of entering cleanly from underneath.
        recycled.text = root.nextLine
        recycled.role = 4
        recycled.animatingEnabled = false
        recycled.y = next.y + next.measuredHeight + root.lineGap
        recycled.animatingEnabled = true

        // Grow/shrink between the small and big sizes is done as a scale
        // transform rather than animating font.pixelSize directly (see the
        // holder delegate below for why) -- so each holder needs to jump to
        // the scale that makes it *still look* like its old size, then ease
        // back to 1.0 once the role (and so target size) has changed.
        items.forEach(function (h) {
            var oldSize = h.role === 2 ? root.bigSize : root.smallSize
            h.role = h.role - 1
            var newSize = h.role === 2 ? root.bigSize : root.smallSize
            var wasAnimating = h.animatingEnabled
            h.animatingEnabled = false
            h.scale = oldSize / newSize
            h.animatingEnabled = wasAnimating
            h.scale = 1.0
        })

        applyLayout(computeLayout(), true)
    }

    Item {
        id: viewport
        anchors.fill: parent
        clip: true
        visible: root.hasLyrics

        readonly property real center: height / 2

        onWidthChanged: root.resnap()
        onHeightChanged: root.resnap()

        Connections {
            target: Theme
            function onUiScaleChanged() { root.resnap() }
        }

        Repeater {
            id: repeater
            model: 4

            delegate: Item {
                id: holder
                property int role: 2
                property bool animatingEnabled: false
                property alias measuredHeight: measurer.height
                property alias text: visibleText.text

                width: viewport.width
                height: visibleText.height
                transformOrigin: Item.Left

                // Growing/shrinking between line sizes is a `scale` step
                // (see rotate()) rather than an animated font.pixelSize:
                // pixelSize is bound directly below and changes instantly,
                // so a word-wrapped Text only re-shapes once per role
                // change instead of on every frame of the transition --
                // animating that reflow every frame is real, visible cost
                // on the Pi's GPU and was the main source of the choppy
                // scroll. `scale` is a pure compositor transform, so the
                // same "growing into place" look now costs nothing extra
                // per frame.
                Behavior on scale { enabled: holder.animatingEnabled; NumberAnimation { duration: root.animDuration; easing.type: Easing.OutCubic } }

                // Plain solid-colour renderer, used for every role except
                // the one currently being sung (role 2, see the karaoke
                // Flow below): role 1 (previous line) already finished
                // being sung, so it's the "sung" colour throughout; role 3
                // (next line, and 0/4 which are invisible anyway) hasn't
                // started, so it's the "pending" colour throughout. Neither
                // needs a per-word breakdown -- only the active line is
                // ever partway through.
                Text {
                    id: visibleText
                    width: parent.width
                    wrapMode: Text.WordWrap
                    color: holder.role <= 1 ? root.sungColor : root.pendingColor
                    font.family: Theme.fontFamily
                    font.weight: Font.DemiBold
                    opacity: root.targetOpacity(holder.role)
                    font.pixelSize: holder.role === 2 ? root.bigSize : root.smallSize
                    visible: holder.role !== 2

                    Behavior on opacity { enabled: holder.animatingEnabled; NumberAnimation { duration: root.animDuration; easing.type: Easing.OutCubic } }
                }

                // Word-by-word wipe for the line currently being sung. Each
                // word is a base copy in root.pendingColor with an
                // identical copy in root.sungColor stacked on top, clipped
                // to a width that grows with that word's estimated
                // progress -- the classic two-layer karaoke-text technique,
                // costing nothing beyond a per-word Item and a clip, no
                // shaders. The clip's Behavior interpolates continuously
                // between root.lineProgress's real samples (a few times a
                // second, from Main.qml's poll timer), which is what turns
                // those discrete samples into what reads as a smooth wipe.
                Flow {
                    id: karaoke
                    width: parent.width
                    visible: holder.role === 2
                    opacity: root.targetOpacity(holder.role)
                    spacing: 0.3 * root.bigSize

                    Behavior on opacity { enabled: holder.animatingEnabled; NumberAnimation { duration: root.animDuration; easing.type: Easing.OutCubic } }

                    property var words: holder.role === 2 ? root.wordThresholds(visibleText.text) : []

                    Repeater {
                        model: karaoke.words

                        delegate: Item {
                            id: wordItem
                            width: base.implicitWidth
                            height: base.implicitHeight

                            Text {
                                id: base
                                text: modelData.word
                                color: root.pendingColor
                                font.family: Theme.fontFamily
                                font.weight: Font.DemiBold
                                font.pixelSize: root.bigSize
                            }

                            Item {
                                width: wordItem.width * Math.max(0, Math.min(1,
                                    (root.lineProgress - modelData.start) / Math.max(0.0001, modelData.end - modelData.start)))
                                height: wordItem.height
                                clip: true

                                Behavior on width { NumberAnimation { duration: 260; easing.type: Easing.Linear } }

                                Text {
                                    text: modelData.word
                                    color: root.sungColor
                                    font.family: Theme.fontFamily
                                    font.weight: Font.DemiBold
                                    font.pixelSize: root.bigSize
                                }
                            }
                        }
                    }
                }

                // Hidden twin: same text, same width, but its font snaps
                // straight to the target size with no Behavior, so its
                // height is always the *final* answer, never a mid-ease one.
                // Bound to the plain-text word-wrap layout rather than the
                // karaoke Flow above -- both wrap the same space-separated
                // words at the same width/font, so their heights agree in
                // practice, and this keeps the layout math independent of
                // which one happens to be showing for role 2 right now.
                Text {
                    id: measurer
                    width: parent.width
                    wrapMode: Text.WordWrap
                    font.family: Theme.fontFamily
                    font.weight: Font.DemiBold
                    font.pixelSize: holder.role === 2 ? root.bigSize : root.smallSize
                    text: visibleText.text
                    visible: false
                }

                Behavior on y { enabled: holder.animatingEnabled; NumberAnimation { duration: root.animDuration; easing.type: Easing.OutCubic } }

                Component.onCompleted: {
                    role = [0, 1, 2, 3][index]
                    text = ["", root.previousLine, root.currentLine, root.nextLine][index]
                    if (index === 3) {
                        root.resnap()
                        for (var i = 0; i < 4; i++) repeater.itemAt(i).animatingEnabled = true
                    }
                }
            }
        }
    }

    Text {
        anchors.centerIn: parent
        visible: !root.hasLyrics
        text: "No lyrics found"
        color: app.track.textIsDark ? Theme.colorTextSecondaryOnLight : Theme.colorTextSecondary
        font.family: Theme.fontFamily
        font.pixelSize: 16 * Theme.uiScale
    }
}
