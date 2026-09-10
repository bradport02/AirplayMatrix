import QtQuick 2.15
import QtGraphicalEffects 1.15

// Full-bleed backdrop. Idle: flat black -- no gradient, nothing derived
// from artwork, because there's no track to derive anything from (also
// deliberately the cheapest possible thing to have on screen, since it's
// the state this build sits in the most). Paused/playing: the current
// artwork, blurred and darkened, standing in for the window chrome.
//
// Qt5 note: this is a port of app/qml/Background.qml, which uses Qt6's
// MultiEffect for both the blur and a saturation/brightness colour-grade in
// one pass. QtGraphicalEffects (Qt5) has no single equivalent combining
// blur with colour grading, and stacking a second full-screen ShaderEffect
// purely for a 0.4 saturation / 0.05 brightness tweak is exactly the kind
// of extra per-frame GPU pass this port can't spend on unproven ARM1176
// hardware -- so that grading is dropped here and only the blur (FastBlur,
// in place of MultiEffect's blurEnabled/blurMax) is kept. The black scrim
// right below already does the legibility work the brightness tweak was
// for; losing the saturation dip is a minor, deliberate look difference,
// not a bug.
//
// This Qt5 build also drops app/qml/Background.qml's ambient colour-wash
// layer entirely (four extra soft colour blobs, each its own FastBlur pass,
// fading in on top of the blurred artwork while playing) rather than
// porting it -- that's four continuous GPU blur passes stacked on top of
// the artwork blur below, for a purely decorative effect, which is real
// per-frame cost the Zero WH's ARM1176/VideoCore IV can't spare on top of
// shairport-sync's real-time audio path. The blurred-artwork backdrop
// (kept below) is the one blur pass that's carrying its own weight -- it's
// the actual chrome, not an extra layer on top of it.
Item {
    id: root

    // Set by Main.qml from the same showTrack value the now-playing panel
    // uses. The blurred backdrop is as much "the current track" as the
    // artwork tile is, so it has to fade with it -- previously it swapped
    // the instant the new cover decoded, which meant the panel faded
    // politely while the entire background cut to the next song behind it.
    property bool showArtwork: false

    // How much smaller than the screen the blur is computed at. 4 puts a
    // 1080p backdrop through a 480x270 blur.
    //
    // This was 6 (320x180) when the scrim below was still a full-screen
    // blend. Folding that into the stage freed roughly two million blended
    // pixels per frame, which buys a finer blur far more usefully than it
    // buys anything else -- the backdrop is the one surface here whose
    // resolution is actually below what it is drawn at, since the artwork
    // tile already decodes at its exact on-screen size and the GPU caps
    // textures at 2048 anyway. Still a fraction of the cost of blurring at
    // full resolution, which is what made crossfades stutter.
    readonly property int blurDownscale: 4

    Rectangle {
        anchors.fill: parent
        color: Theme.colorIdleBackground
    }

    // Backdrop artwork, kept as two alternating layers so one cover can
    // dissolve into the next (transition_mode "crossfade") instead of the
    // background cutting while the panel in front of it fades.
    //
    // There is exactly ONE FastBlur, and the two covers cross-fade *inside*
    // its source item -- so the backdrop dissolves with the artwork while
    // still costing a single blur pass. An earlier version gave each cover
    // its own full-screen blur and cross-faded those: two 1920x1080 blur
    // passes at once made the whole device lag and starved shairport-sync
    // badly enough to drop the audio, so that route is closed on this
    // hardware.
    //
    // Two images are kept regardless, so the swap happens *after* the new
    // one has decoded -- reassigning a single Image's source would blank
    // the backdrop for the length of the decode.
    //
    // Both decode at 480px, matching the blur stage they feed: this is only
    // ever seen through a heavy blur, which destroys far more detail than
    // the downscale does.
    QtObject {
        id: backdrop
        property bool frontIsA: true
        // Matches the artwork tile, so backdrop and cover dissolve together.
        readonly property int duration:
            app.settings.transitionMode === "crossfade" ? app.settings.crossfadeMs : 0
    }

    // Same effective source as AlbumArt, so the backdrop turns over in the
    // same frame as the cover rather than a beat behind it.
    readonly property string effectiveSource:
        (app.track.readyToTransition && app.track.pendingArtworkSource)
        ? app.track.pendingArtworkSource
        : app.track.artworkSource

    onEffectiveSourceChanged: _loadBackdrop()

    function _loadBackdrop() {
        var src = root.effectiveSource
        var back = backdrop.frontIsA ? imageB : imageA
        var front = backdrop.frontIsA ? imageA : imageB
        if (src === front.source) return
        if (src === "") {
            // Nothing to decode, so no Ready is coming -- swap immediately.
            back.source = ""
            backdrop.frontIsA = !backdrop.frontIsA
            return
        }
        back.source = src
    }

    function _backdropReady(image) {
        var back = backdrop.frontIsA ? imageB : imageA
        if (image === back && image.status === Image.Ready) {
            backdrop.frontIsA = !backdrop.frontIsA
        }
    }

    Component.onCompleted: _loadBackdrop()

    // Both layers live inside one container, and the container is what the
    // blur below captures -- so the two covers cross-fade *before* being
    // blurred, and there is still only a single blur pass. The alternative
    // (a blur each, cross-faded) is what previously ran two full-screen
    // blurs at once and starved the audio thread badly enough to drop
    // sound, so it is not an option on this hardware.
    // The blurred backdrop and its legibility scrim, faded as one piece so
    // the darkening doesn't linger over a background that's already gone.
    Item {
        id: artworkLayer
        anchors.fill: parent
        opacity: root.showArtwork ? 1 : 0
        visible: opacity > 0

        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow; easing.type: Easing.InOutQuad } }

        // The blur runs on a deliberately tiny stage which is then scaled
        // up to fill the screen, rather than blurring at 1920x1080
        // directly. Blur cost scales with area, so this is roughly
        // BLUR_DOWNSCALE^2 -- about 36x -- cheaper.
        //
        // That matters far more than it used to. While the two covers are
        // crossfading, the blur's source item is *changing every frame*, so
        // the blur cannot be computed once and reused -- it re-renders for
        // the entire length of the transition. At full resolution that is a
        // full-screen blur per frame on a VideoCore IV, which is what made
        // the crossfade choppy. Upscaling costs nothing by comparison, and
        // is invisible here because the thing being magnified is already a
        // heavy blur.
        Item {
            id: blurStage
            width: Math.max(1, Math.round(parent.width / root.blurDownscale))
            height: Math.max(1, Math.round(parent.height / root.blurDownscale))
            transformOrigin: Item.TopLeft
            scale: root.blurDownscale

            // Flatten the stage into a single texture before it is scaled
            // up. Without this, `scale` reduces nothing that costs anything:
            // a scaled Rectangle still writes every one of its on-screen
            // pixels, so the blur and the scrim below were two separate
            // full-screen blends per frame regardless of the stage size.
            // Only FastBlur got cheaper, because it renders into its own
            // buffer at the item's size.
            //
            // Layered, the blur and the scrim composite together at
            // 480x270 and reach the screen as one textured quad -- which is
            // what actually removes a full-screen blend from every frame of
            // a transition, when the backdrop is redrawing continuously.
            layer.enabled: true
            layer.smooth: true

            Item {
                id: backdropSource
                anchors.fill: parent
                visible: false

                Image {
                    id: imageA
                    anchors.fill: parent
                    sourceSize.width: 480
                    sourceSize.height: 480
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: false
                    opacity: backdrop.frontIsA ? 1 : 0
                    onStatusChanged: root._backdropReady(imageA)
                    Behavior on opacity { NumberAnimation { duration: backdrop.duration; easing.type: Easing.InOutQuad } }
                }

                Image {
                    id: imageB
                    anchors.fill: parent
                    sourceSize.width: 480
                    sourceSize.height: 480
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: false
                    opacity: backdrop.frontIsA ? 0 : 1
                    onStatusChanged: root._backdropReady(imageB)
                    Behavior on opacity { NumberAnimation { duration: backdrop.duration; easing.type: Easing.InOutQuad } }
                }
            }
            FastBlur {
                anchors.fill: parent
                source: backdropSource
                // Radius is in stage pixels, so it has to come down by the
                // same factor to keep the on-screen blur the same size.
                radius: 64 * Theme.uiScale / root.blurDownscale
            }

            // Darkens the blurred artwork so foreground text stays legible,
            // without washing out its colour the way a heavier scrim would.
            //
            // Deliberately *inside* the scaled-down stage. It is a flat
            // black wash, so drawing it here and letting it scale up with
            // the blur is pixel-identical to drawing it full-screen -- but
            // it costs a 320x180 blend instead of a 1920x1080 one. During a
            // crossfade the backdrop redraws every frame, so that is a
            // couple of million blended pixels per frame saved on a GPU
            // whose limit here is fill rate, not memory or CPU.
            Rectangle {
                anchors.fill: parent
                color: "black"
                opacity: 0.35
            }

            // Vignette so the top/bottom bars read clearly over artwork.
            // Inside the layer with everything else, so it composites at
            // stage resolution and costs nothing extra on screen rather
            // than being a second full-screen gradient blend every frame.
            //
            // It also no longer needs its own sessionActive binding: it
            // lives with the artwork now, and artwork is the only thing it
            // was ever meant to darken. The idle screen stays flat, which
            // is what it always wanted to be.
            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    orientation: Gradient.Vertical
                    GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.35) }
                    GradientStop { position: 0.18; color: Qt.rgba(0, 0, 0, 0) }
                    GradientStop { position: 0.82; color: Qt.rgba(0, 0, 0, 0) }
                    GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.45) }
                }
            }
        }
    }
}
