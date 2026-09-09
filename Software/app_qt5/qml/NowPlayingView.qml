import QtQuick 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    property string previousLine: ""
    property string currentLine: ""
    property string nextLine: ""
    property bool creditsMode: false
    // Driven by Main.qml, which folds together the controller's transition
    // state and artworkReady below -- see its comment. Both this panel and
    // Background.qml's blurred backdrop fade off the same value.
    property bool showTrack: false
    readonly property bool artworkReady: albumArt.ready
    readonly property bool crossfade: app.settings.transitionMode === "crossfade"

    // One knob for every stage of a crossfade, tunable from the web UI
    // (display_settings.crossfade_seconds). Fade mode keeps the app's
    // standard animation length, since there it's just the panel fading.
    readonly property int transitionMs: crossfade ? app.settings.crossfadeMs : Theme.durationSlow

    // Idle state -- no AirPlay session open. Deliberately static: this is
    // the screen that's on-air the most (sitting there waiting for a
    // session to start), so on the Zero WH's single ARM1176 core it's the
    // one that most needs to cost the GPU nothing at all -- no rings, no
    // pulsing dot, no Behavior-driven animation, just plain white text on
    // the flat black idle backdrop (Background.qml/Theme.colorIdleBackground).
    // bridgeConnected tracks the metadata pipe's own connection, not
    // whether shairport-sync itself is running -- in practice that pipe
    // only reports connected right as a session starts, so the false
    // branch is what's on screen for virtually all of normal idle time,
    // not an actual fault. "Waiting for connection" reflects that; a
    // genuinely offline receiver isn't something this app can tell apart
    // from a receiver that's simply idle, so it doesn't try to.
    //
    // It stays up until the first track of a session has actually been
    // published -- not merely until a session exists. A session opens
    // several hundred milliseconds before there's a title, let alone
    // artwork, so switching on sessionActive alone put an empty panel on
    // screen and filled it in field by field. Now this screen holds, then
    // cross-fades against the panel below once there's a complete track to
    // show.
    ColumnLayout {
        id: idleScreen
        anchors.centerIn: parent
        opacity: (app.track.sessionActive && app.track.contentReady) ? 0 : 1
        visible: opacity > 0
        spacing: Theme.spacingXs

        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow; easing.type: Easing.InOutQuad } }

        // A device is here -- either announced itself ("conn"/"snam", which
        // land about half a second before it starts streaming) or is already
        // streaming. Reporting on the earlier of the two means the TV
        // acknowledges the connection at the same moment the phone does,
        // rather than staying on "Waiting for AirPlay" until play is
        // pressed.
        //
        // sessionActive is included, not just clientConnected, for a
        // reason: this screen is still fading out when the first track
        // publishes, and a condition that flipped false right then would
        // change the text back to "Waiting for AirPlay" mid-fade, which
        // reads as the connection having dropped.
        readonly property bool connecting: app.track.clientConnected || app.track.sessionActive

        Text {
            Layout.alignment: Qt.AlignHCenter
            // Only once audio is actually streaming is there a track to be
            // loading -- between connecting and pressing play there is
            // nothing incoming to report, so this line stays empty rather
            // than claiming to be loading something.
            text: app.track.sessionActive
                  ? "Loading track…"
                  : (idleScreen.connecting ? "" : "Discoverable: " + app.deviceName)
            visible: text.length > 0
            color: Theme.colorTextSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 14 * Theme.uiScale
        }

        Text {
            Layout.alignment: Qt.AlignHCenter
            // shairport-sync sends the sender's name ("snam") during the
            // connection handshake, ahead of the session even opening, so
            // it is reliably there by the time this shows. Falls back to
            // the generic wording if a sender ever omits it.
            text: idleScreen.connecting
                  ? (app.track.clientName
                     ? "Connection received from " + app.track.clientName
                     : "Connection received")
                  : (app.track.bridgeConnected ? "Waiting for AirPlay" : "Waiting for connection")
            color: Theme.colorTextPrimary
            font.family: Theme.fontFamily
            font.pixelSize: 20 * Theme.uiScale
            font.weight: Font.Medium
        }
    }

    // Now-playing state: artwork on the left, details/lyrics on the right.
    // The artwork sizes off the row's actual available height (already a
    // live reflection of the window size) rather than a fixed pixel cap, so
    // it scales up on a big/fullscreen window instead of staying pinned
    // small.
    //
    // Everything it displays comes from
    // TrackController's *published* snapshot, which only changes while this
    // is fully faded out -- so a track change is: fade out the old track
    // intact, swap every field at once behind the fade, fade the new one
    // in. onOpacityChanged is the other half of that contract: reaching
    // zero is what tells the controller it's safe to swap (see
    // TrackController.fadeOutComplete), so the swap can never race the
    // fade. `visible: opacity > 0` matters for more than tidiness --
    // Qt Quick skips invisible subtrees entirely, so a faded panel costs
    // nothing to have on screen.
    RowLayout {
        id: trackPanel
        anchors.fill: parent
        anchors.margins: Theme.spacingXl
        visible: opacity > 0
        opacity: root.showTrack ? 1 : 0
        spacing: Theme.spacingXl

        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow; easing.type: Easing.InOutQuad } }

        // Guarded on trackChanging: opacity also passes through 0 at the
        // *start* of a fade back in, and reporting a fade-out there would
        // be a lie.
        onOpacityChanged: if (opacity <= 0 && app.track.trackChanging) app.track.fadeOutComplete()

        AlbumArt {
            id: albumArt
            Layout.preferredWidth: parent.height
            Layout.preferredHeight: Layout.preferredWidth
            Layout.alignment: Qt.AlignVCenter
        }

        // Text side of the panel: title/artist and lyrics. In "crossfade"
        // mode this is the part that actually fades on a track change --
        // the artwork and backdrop stay put and dissolve underneath it, so
        // the screen never empties. It goes out first, the snapshot is
        // swapped while it's invisible (see onOpacityChanged below), and it
        // comes back once the incoming artwork is on screen.
        //
        // Opacity only, never `visible`: an invisible child is dropped from
        // the Layout entirely, which would hand its width to the artwork
        // and make the cover resize mid-transition.
        ColumnLayout {
            id: textColumn
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.alignment: Qt.AlignVCenter
            spacing: Theme.spacingLg

            // Three phases, and the first is the point of this expression:
            //   1. change detected, data still arriving -> stay VISIBLE.
            //      The outgoing track keeps the screen while the new
            //      artwork makes its way over (~1.5s after the title),
            //      instead of fading out and leaving the display empty for
            //      the wait, which is what made it feel unfluid.
            //   2. readyToTransition -> everything here fades out together,
            //      and reaching zero is what releases the snapshot swap.
            //   3. swapped -> stays out until the incoming artwork has
            //      decoded, then comes back in as the artwork dissolves.
            opacity: (root.crossfade
                      && (app.track.readyToTransition || !root.artworkReady)) ? 0 : 1

            Behavior on opacity {
                NumberAnimation { duration: root.transitionMs; easing.type: Easing.InOutQuad }
            }

            // Crossfade mode's equivalent of the panel-level handshake
            // below: nothing else is leaving the screen, so this is what
            // tells TrackController it's safe to swap the track over.
            onOpacityChanged: {
                if (root.crossfade && opacity <= 0 && app.track.trackChanging) {
                    app.track.fadeOutComplete()
                }
            }

            // Title/artist/album -- the "song details" web UI toggle
            // (Software/display_settings.py, default on). Layouts skip
            // invisible children entirely, so hiding this just lets
            // LyricsPanel below take the freed space rather than leaving a
            // blank gap.
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs
                visible: app.settings.showDetails

                Text {
                    text: app.track.title || "—"
                    // Swaps to dark ink over light album art -- see
                    // TrackController.textIsDark / encoder.legible_text_is_dark.
                    color: app.track.textIsDark ? Theme.colorTextPrimaryOnLight : Theme.colorTextPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 28 * Theme.uiScale
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: [app.track.artist, app.track.album].filter(function (s) { return s.length > 0 }).join(" — ")
                    color: app.track.textIsDark ? Theme.colorTextSecondaryOnLight : Theme.colorTextSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 16 * Theme.uiScale
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    visible: text.length > 0
                }
            }

            // Lyrics -- the "lyrics" web UI toggle (default off). Fully
            // absent from the tree when off, not just hidden: LyricsPanel
            // is the CPU-heaviest thing this app does (per-frame scroll/
            // reflow), which is the whole reason this toggle exists on the
            // Zero WH build. LyricsController itself also stops fetching
            // when this is off (see app_qt5/lyrics_controller.py), so
            // turning it off is a clean, complete stop, not just a hidden
            // panel still doing the work behind it.
            LyricsPanel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: app.settings.showLyrics
                previousLine: root.previousLine
                currentLine: root.currentLine
                nextLine: root.nextLine
                creditsMode: root.creditsMode
                hasLyrics: app.lyrics.hasLyrics
                searched: app.lyrics.searched
            }
        }
    }
}
