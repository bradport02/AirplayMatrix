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

    Rectangle {
        anchors.fill: parent
        color: Theme.colorIdleBackground
    }

    Image {
        id: artImage
        anchors.fill: parent
        source: app.track.artworkSource
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: false
        visible: false
        opacity: 0

        onSourceChanged: opacity = 0
        onStatusChanged: if (status === Image.Ready) opacity = 1

        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow; easing.type: Easing.OutCubic } }
    }

    FastBlur {
        anchors.fill: parent
        source: artImage
        visible: app.track.artworkSource !== ""
        radius: 64 * Theme.uiScale
    }

    // Darkens the blurred artwork so foreground text stays legible, without
    // washing out its colour the way a heavier scrim would.
    Rectangle {
        anchors.fill: parent
        color: "black"
        opacity: app.track.artworkSource !== "" ? 0.35 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow } }
    }

    // Vignette so the top/bottom bars read clearly over artwork -- only
    // relevant once there's actually a session's worth of content behind
    // it. Idle is meant to be flat black with nothing layered over it at
    // all, so this stays fully off there rather than leaving a faint
    // light/dark/light banding across a background that's supposed to
    // read as one plain colour.
    Rectangle {
        anchors.fill: parent
        opacity: app.track.sessionActive ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow } }
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.35) }
            GradientStop { position: 0.18; color: Qt.rgba(0, 0, 0, 0) }
            GradientStop { position: 0.82; color: Qt.rgba(0, 0, 0, 0) }
            GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.45) }
        }
    }
}
