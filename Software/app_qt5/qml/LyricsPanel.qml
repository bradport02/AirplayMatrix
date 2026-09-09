import QtQuick 2.15

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
// This whole panel only exists in the tree while app.settings.showLyrics is
// true -- see NowPlayingView.qml, which is also what stops LyricsController
// fetching in the first place. It's the CPU-heaviest thing in this app
// (per-frame text reflow/scroll), which is exactly why the Zero WH port
// makes it optional in the first place.
//
// Colour tells you where you are in the song, borrowed from Apple Music's
// lyrics: lines still to come are dimmed, the line playing now and the ones
// already sung are bright. The Pi 5 build (app/qml/LyricsPanel.qml) goes
// further and wipes the active line word-by-word; this build deliberately
// does not -- see the Text delegate below for why that costs more than this
// device can spare.
Item {
    id: root
    property string previousLine: ""
    property string currentLine: ""
    property string nextLine: ""
    property bool hasLyrics: false
    // True once playback reaches the end-of-song credits (see
    // LyricsController.creditsActive). Credits are a block to be read at a
    // glance, not lyrics sung one line at a time, so while this is set the
    // whole window is shown at the small size, in full white, with nothing
    // dimmed and nothing growing -- they still scroll up into the active
    // position exactly like lyric lines do.
    property bool creditsMode: false
    // Whether a lookup has actually come back for the current track.
    // See LyricsController.searched -- without this the panel says
    // "No lyrics found" during every fetch, then replaces it with the
    // lyrics a moment later.
    property bool searched: false

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

    // Role 1 (already sung) sits at full opacity alongside role 2 (playing
    // now): with colour carrying the sung/pending distinction, fading the
    // previous line too would say "this is receding" at the same time its
    // white colour says "this has been played" -- two different signals for
    // one state. Only role 3 (still to come) is dimmed, which reinforces
    // pendingColor rather than fighting it. Role 2 is still distinguished
    // from role 1, just by size rather than by fade.
    // The active line is the only one that grows, and only for lyrics --
    // credits stay at the small size in every role.
    function sizeForRole(role) {
        return (role === 2 && !creditsMode) ? bigSize : smallSize
    }

    function targetOpacity(role) {
        if (creditsMode) return (role >= 1 && role <= 3) ? 1.0 : 0.0
        return (role === 1 || role === 2) ? 1.0 : (role === 3 ? 0.5 : 0.0)
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

    // Entering or leaving credits changes every holder's target font size,
    // and the layout is computed from those sizes rather than continuously
    // tracked -- so it has to be recomputed here. In practice a rotate()
    // usually happens on the same tick (the "Credits:" line becoming
    // active), which does this anyway; this covers the case where it
    // doesn't, rather than leaving the lines spaced for the old sizes.
    onCreditsModeChanged: resnap()

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
            var oldSize = root.sizeForRole(h.role)
            h.role = h.role - 1
            var newSize = root.sizeForRole(h.role)
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

                // Whole-line solid-colour renderer -- one Text per line, for
                // every role. Colour is the only thing that distinguishes
                // where we are in the song: role 1 (already sung) and role 2
                // (the line playing right now) are the bright "sung" colour;
                // role 3 (the line still to come, and 0/4 which are
                // invisible anyway) is the dimmed "pending" colour.
                //
                // Zero WH build only: this deliberately does NOT do the
                // word-by-word "karaoke" wipe that app/qml/LyricsPanel.qml
                // (Pi 5 build) does -- see that file for it. The wipe means
                // a per-word Item + clip whose width is re-evaluated on
                // every lyrics poll for the whole time a line is playing,
                // and on this single-core ARM1176 with no GPU compositing
                // that constant reflow is exactly the kind of always-on
                // render work this device cannot spare. The colour split
                // above gives the same at-a-glance "here's where we are"
                // read for one Text per line and no per-frame work.
                Text {
                    id: visibleText
                    width: parent.width
                    wrapMode: Text.WordWrap
                    color: (root.creditsMode || holder.role <= 2) ? root.sungColor : root.pendingColor
                    font.family: Theme.fontFamily
                    font.weight: Font.DemiBold
                    opacity: root.targetOpacity(holder.role)
                    font.pixelSize: root.sizeForRole(holder.role)

                    Behavior on opacity { enabled: holder.animatingEnabled; NumberAnimation { duration: root.animDuration; easing.type: Easing.OutCubic } }
                }

                // Hidden twin: same text, same width, but its font snaps
                // straight to the target size with no Behavior, so its
                // height is always the *final* answer, never a mid-ease one.
                Text {
                    id: measurer
                    width: parent.width
                    wrapMode: Text.WordWrap
                    font.family: Theme.fontFamily
                    font.weight: Font.DemiBold
                    font.pixelSize: root.sizeForRole(holder.role)
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
        visible: !root.hasLyrics && root.searched
        text: "No lyrics found"
        color: app.track.textIsDark ? Theme.colorTextSecondaryOnLight : Theme.colorTextSecondary
        font.family: Theme.fontFamily
        font.pixelSize: 16 * Theme.uiScale
    }
}
