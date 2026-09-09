import QtQuick 2.15

// Two stacked Images that alternate, so one artwork can dissolve into the
// next without the screen passing through an empty state in between.
//
// The trick is simply never to reassign the layer that's currently showing:
// a new `source` is loaded into the *back* layer while the front one carries
// on displaying the outgoing artwork, and the two swap opacity only once the
// back layer reports Image.Ready. That ordering is the whole point -- flipping
// on assignment instead would show a blank layer for however long the decode
// takes, which on this hardware is a good fraction of a second and is exactly
// the "fades to nothing" look this component exists to avoid.
//
// Deliberately not implemented with ShaderEffectSource: capturing a live item
// into a texture is unreliable here for any source that has children of its
// own (see AlbumArt.qml's note on the same limitation biting the drop
// shadow), and two plain Images cost far less on this GPU than a capture
// would anyway.
Item {
    id: root

    property string source: ""
    // A source to decode into the offscreen layer *ahead of time*, without
    // showing it. The incoming cover is known about a second and a half
    // before it should appear (TrackController.pendingArtworkSource), so
    // warming it here means the swap below is instant rather than stopping
    // to decode a full cover at exactly the moment the transition starts --
    // which is what made the crossfade stutter.
    property string preloadSource: ""
    property int decodeSize: 256
    property int fillMode: Image.PreserveAspectCrop
    property int duration: Theme.durationSlow
    // Whether the *requested* source is the one now on screen. Tracked with
    // an explicit flag rather than read off the front layer's status: during
    // a change the front layer is still the outgoing artwork, and it is
    // perfectly Ready, so asking it would answer "yes" while the incoming
    // cover was still decoding -- defeating the whole point for callers that
    // gate a fade-in on the artwork actually being there.
    property bool _pending: false
    readonly property bool ready: !_pending && front.status !== Image.Loading

    // Which of the two is currently the visible ("front") layer.
    property bool frontIsA: true
    readonly property Image front: frontIsA ? imageA : imageB
    readonly property Image back: frontIsA ? imageB : imageA

    // url and string compare unequal without this.
    function _same(a, b) { return String(a) === String(b) }

    onPreloadSourceChanged: {
        if (!preloadSource) return
        if (_same(preloadSource, front.source) || _same(preloadSource, back.source)) return
        // Decode only. The flip stays with onSourceChanged, so warming a
        // cover never puts it on screen early.
        back.source = preloadSource
    }

    onSourceChanged: {
        if (_same(source, front.source)) { _pending = false; return }
        // Empty source means "no artwork" -- there is nothing to decode and
        // no Ready ever arrives, so swap straight away rather than waiting.
        if (source === "") {
            back.source = ""
            _pending = false
            frontIsA = !frontIsA
            return
        }
        if (!_same(back.source, source)) back.source = source
        if (back.status === Image.Ready) {
            // Already warmed -- swap on the spot, no decode pause.
            _pending = false
            frontIsA = !frontIsA
        } else {
            _pending = true
        }
    }

    function _onBackReady(image) {
        // Guard against a stale layer finishing after another change: only
        // the back layer, and only while a change is actually outstanding.
        if (!_pending || image !== back) return
        if (image.status === Image.Ready) {
            _pending = false
            frontIsA = !frontIsA
        } else if (image.status === Image.Error) {
            // Give up rather than waiting forever. `ready` gates the text
            // column coming back on screen, so a cover that never loads
            // used to leave the title and lyrics hidden for the rest of the
            // track. Keep showing the outgoing artwork -- stale artwork is
            // a far smaller problem than a blank panel.
            _pending = false
        }
    }

    Image {
        id: imageA
        anchors.fill: parent
        sourceSize.width: root.decodeSize
        sourceSize.height: root.decodeSize
        fillMode: root.fillMode
        asynchronous: true
        cache: false
        opacity: root.frontIsA ? 1 : 0
        visible: opacity > 0
        onStatusChanged: root._onBackReady(imageA)
        Behavior on opacity { NumberAnimation { duration: root.duration; easing.type: Easing.InOutQuad } }
    }

    Image {
        id: imageB
        anchors.fill: parent
        sourceSize.width: root.decodeSize
        sourceSize.height: root.decodeSize
        fillMode: root.fillMode
        asynchronous: true
        cache: false
        opacity: root.frontIsA ? 0 : 1
        visible: opacity > 0
        onStatusChanged: root._onBackReady(imageB)
        Behavior on opacity { NumberAnimation { duration: root.duration; easing.type: Easing.InOutQuad } }
    }
}
