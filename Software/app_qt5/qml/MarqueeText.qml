import QtQuick 2.15

// One line of text that scrolls sideways when -- and only when -- it is
// wider than the space it has been given, in place of eliding it away with
// an ellipsis. A title or album name that doesn't fit is exactly the case
// where the missing characters matter most, so cutting them is the one
// behaviour worth spending frames on.
//
// Everything below exists to make "only when" literally true, because a
// marquee is the one thing this build has so far refused to have: a
// *continuous* per-frame animation. Background.qml explains what that
// budget actually is on the Zero WH -- two full-screen blur passes at once
// starved shairport-sync badly enough to drop audio -- and while scrolling
// a strip of glyphs is nowhere near that weight, it is not free either.
//
// The cost is not the glyphs. A line of text is one batch of quads drawn
// from the shared glyph atlas; at 16px that is a few thousand fragments
// against the roughly two million the full-screen composite already draws.
// The cost is that *any* running QML animation keeps Qt's animation driver
// -- and therefore the render loop -- ticking at vsync, so the whole window
// recomposites every frame for as long as one is alive, whether or not that
// frame differs from the last. So the rule here is not "make the scroll
// cheap", it is "have no animation running unless text is genuinely moving
// on screen":
//
//   * text that fits    -> overflowing is false, nothing ever starts, and
//                          this renders as the plain left-aligned Text it
//                          replaced, node for node.
//   * between passes    -> the dwell is a stopped Timer, NOT a
//                          PauseAnimation. A PauseAnimation is a running
//                          animation; it would hold the render loop open
//                          for its entire length and pay full-screen
//                          recomposites to display a stationary word. A
//                          Timer wakes the GUI thread once and draws
//                          nothing.
//   * off screen        -> `visible` is effective visibility, so the idle
//                          screen (trackPanel goes visible:false with its
//                          opacity) and the "song details" web UI toggle
//                          both stop this dead without either of them
//                          having to know it exists. That matters most for
//                          the idle screen, which is what this device sits
//                          on for the majority of its uptime.
//
// It scrolls out and back rather than wrapping the string around with a
// second copy: a ping-pong needs one Text node and no seam, where a
// continuous ticker needs two copies drawn at once and never rests. Reading
// the end of an album name twice a minute is worth more here than a
// perfectly seamless loop.
Item {
    id: root

    property alias text: label.text
    property alias color: label.color
    property alias font: label.font
    property alias style: label.style
    property alias styleColor: label.styleColor

    // Reading rate, expressed as a multiple of the font size rather than as
    // a fixed pixel rate. Roughly eight characters a second at any size,
    // which is what keeps the 28px title and the 16px album line reading at
    // the *same* pace instead of the smaller one appearing to race; a flat
    // px/s figure tuned for one of them is visibly wrong for the other.
    // pixelSize already carries Theme.uiScale, so this also scales itself
    // across the 720x480 minimum and a fullscreen 1080p window for free.
    property real speed: label.font.pixelSize * 4
    // Held still at each end. Long enough to read a line that has just
    // arrived before it starts moving, and long enough that a line which
    // does overflow spends well under half its time animating.
    property int dwellMs: 3000

    // Deliberately 0, where the Text this replaced reported the full width
    // of the string. That width was pressure on the RowLayout in
    // NowPlayingView: a long title asking for more room than the column had
    // could take it out of the album art's preferred width and resize the
    // cover. Nothing here needs a minimum -- overflow is the normal case,
    // not a failure -- so this asks for nothing and the artwork keeps its
    // size whatever the metadata says.
    implicitWidth: 0
    implicitHeight: label.implicitHeight

    // Only while something is actually hanging over the edge. A clip is a
    // scissor rect rather than a render target, so it is cheap, but it also
    // breaks the renderer's batching, and there is no reason to pay that on
    // the far commoner short-text case where nothing can spill anyway.
    clip: overflowing

    readonly property real overflow: Math.max(0, label.implicitWidth - width)
    // The width > 0 guard is for the first layout pass, where width is
    // still 0 and every string on earth "overflows" -- without it every
    // track would arm a scroll during startup and then disarm it.
    readonly property bool overflowing: width > 0 && overflow >= 1
    readonly property bool scrolling: overflowing && root.visible

    // Which end the next leg is heading for. Legs are driven one at a time
    // from the Timer rather than being a SequentialAnimation, precisely so
    // the gaps between them are dead air and not a PauseAnimation.
    property bool _outbound: false

    function _rewind() {
        leg.stop()
        _outbound = false
        label.x = 0
    }

    // A genuinely new string starts from the beginning. This is also the
    // load-bearing half of the same-album promise in NowPlayingView: on a
    // change that stays on one album, TrackController._publish emits
    // neither artistChanged nor albumChanged, so the artist/album binding
    // never re-evaluates, this never fires, and the line does not so much
    // as blink -- let alone snap back to the start of a scroll it was
    // halfway through. Nothing here is bound to trackChanging or
    // sameAlbumTransition for that reason: a marquee that reset itself on
    // "a track changed" would break exactly the case the transition exists
    // to protect.
    onTextChanged: _rewind()

    onOverflowingChanged: if (!overflowing) _rewind()

    // Window resized, or a font/scale change moved the end of the line.
    // Re-seat the resting position so a line parked at the end doesn't sit
    // at a stale offset; a leg in flight is already animating toward the
    // new value, since `to` reads this live.
    onOverflowChanged: if (!leg.running) label.x = _outbound ? -overflow : 0

    // Land on a clean end state instead of freezing mid-leg. This can only
    // fire while the line is off screen -- `scrolling` drops because
    // visibility did, or because there is no longer anything to scroll, in
    // which case _rewind above has already put x back at 0 -- so the jump
    // is never seen.
    onScrollingChanged: if (!scrolling) leg.complete()

    Text {
        id: label
        // No width binding and no elide: an unconstrained Text sizes to its
        // own content, which is what gives us something to scroll. `x` is
        // the only thing that ever moves, so the glyph run itself is laid
        // out once and reused -- the animation dirties a transform, not a
        // text layout.
        maximumLineCount: 1
    }

    Timer {
        id: dwell
        interval: root.dwellMs
        // Not repeat: true. It re-arms off `leg.running` going false, which
        // means the rest between passes always starts when the previous
        // pass actually finished rather than drifting against a free-running
        // clock.
        running: root.scrolling && !leg.running
        onTriggered: {
            root._outbound = !root._outbound
            leg.restart()
        }
    }

    NumberAnimation {
        id: leg
        target: label
        property: "x"
        to: root._outbound ? -root.overflow : 0
        // Linear, because constant speed is what makes a moving line
        // readable -- an eased scroll reads as the text hesitating. The
        // floor keeps a line that overflows by a handful of pixels from
        // twitching: at full speed that would be a 60ms flick rather than
        // anything a viewer could follow.
        duration: Math.max(Theme.durationSlow,
                           Math.round(root.overflow / Math.max(1, root.speed) * 1000))
        easing.type: Easing.Linear
    }
}
