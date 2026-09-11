import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15
import QtQuick.Window 2.15

ApplicationWindow {
    id: window
    width: 960
    height: 600
    minimumWidth: 720
    minimumHeight: 480
    visible: true
    visibility: Window.FullScreen
    title: "AirPlay Desk Display"
    color: Theme.colorBackground

    // One source of truth for "the current track is on screen", shared by
    // the now-playing panel and the blurred artwork backdrop so the two
    // fade as a single piece rather than the panel fading while the
    // background cuts. It waits on nowPlaying.artworkReady as well as the
    // controller's own trackChanging: the snapshot is published while the
    // panel is invisible, but the Image still has to decode the new cover
    // after that, and fading in before it lands is what made the artwork
    // appear to pop rather than fade.
    // In "crossfade" mode neither the panel nor the backdrop leaves the
    // screen: the artwork dissolves in place (CrossfadeImage) while the
    // text column fades out and back in around it. Gating on
    // trackChanging/artworkReady here would fade the whole lot out
    // underneath that -- which is what made the backdrop drop out
    // mid-crossfade.
    //
    // A change that stays on the same album is the third way through here,
    // and for "fade" mode it is the whole point of the feature: the cover
    // and the blurred backdrop are already the right ones for the incoming
    // track, so taking them off screen and bringing the identical image
    // back would be motion that says nothing. Holding showTrack true keeps
    // both of them exactly where they are, and NowPlayingView narrows the
    // transition to the two things that genuinely differ -- the song title
    // and the lyrics. "crossfade" mode already never leaves the screen, so
    // this changes nothing for it; it reads the same flag lower down to
    // decide *which* text fades.
    //
    // It also makes this the cheapest track change the app can do. Nothing
    // fades, nothing dissolves, and because the same album means the sender
    // re-sends byte-identical cover art, artworkSource never changes value
    // -- so there is no decode, and the single full-screen blur below is
    // not re-rendered even once. See Background.qml on why that blur is the
    // one cost on this device worth going out of the way to avoid.
    //
    // Standby (app.standby -- see AppController._read_standby) outranks all
    // of it. Stopping the receiver kills the metadata writer, so the session
    // does end on its own a moment later, but only a moment: there is a
    // window of up to one poll plus shairport-sync's shutdown in which the
    // last track is still the published snapshot. Leading with standby here
    // means the artwork and the blurred backdrop leave the screen with the
    // same fade they always use, instead of the standby message arriving
    // over the top of a now-fictional now-playing panel. It costs the normal
    // path nothing: it is a term that is false for the entire time the
    // receiver is switched on.
    readonly property bool crossfade: app.settings.transitionMode === "crossfade"
    readonly property bool showTrack: !app.standby
                                      && app.track.sessionActive
                                      && app.track.contentReady
                                      && (window.crossfade
                                          || app.track.sameAlbumTransition
                                          || (!app.track.trackChanging && nowPlaying.artworkReady))

    Background {
        anchors.fill: parent
        showArtwork: window.showTrack
    }

    // Everything sizes off Theme.uiScale rather than fixed pixels, so the
    // layout holds together from the 720x480 minimum up through a maximised
    // or fullscreen window instead of looking tiny/cramped or oversized
    // relative to whatever's actually on screen.
    Binding {
        target: Theme
        property: "uiScale"
        value: Math.max(0.75, Math.min(2.5, Math.min(window.width / 960, window.height / 600)))
    }

    // Single position/lyrics poll shared by the lyrics panel and the
    // playback bar -- see NowPlayingView's docstring on why lyrics stay
    // pull-based rather than a second internal timer per consumer. The
    // lineAt/previousLineAt/nextLineAt calls are skipped entirely while
    // the lyrics toggle is off, on top of LyricsController already
    // skipping its own fetch -- no point paying for three QML->Python
    // calls a tick to look up lines nothing displays.
    QtObject {
        id: poller
        property real position: 0
        property real fraction: app.track.duration > 0 ? position / app.track.duration : 0
        property string previousLine: ""
        property string currentLine: ""
        property string nextLine: ""
        property bool creditsMode: false
    }

    Timer {
        interval: 250
        running: app.track.sessionActive
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            poller.position = app.track.currentPosition()
            if (app.settings.showLyrics) {
                // previous/next must land before currentLine: LyricsPanel's
                // transition reads root.nextLine the instant currentLine
                // changes, so nextLine has to already be fresh by then.
                // Same applies to creditsMode, see below.
                poller.previousLine = app.lyrics.previousLineAt(poller.position)
                poller.nextLine = app.lyrics.nextLineAt(poller.position)
                // creditsMode must also land before currentLine: the
                // rotation triggered by a line change reads the target font
                // size for each role, and credits use the small size in
                // every role -- flipping the mode afterwards would animate
                // the scale to the wrong size first.
                poller.creditsMode = app.lyrics.creditsActive(poller.position)
                poller.currentLine = app.lyrics.lineAt(poller.position)
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        StatusBar {
            Layout.fillWidth: true
            Layout.preferredHeight: implicitHeight
            // Hidden (not just faded) on the idle/waiting screen -- Layouts
            // skip invisible children entirely (see NowPlayingView.qml's
            // comment on the same trick), so this also stops the 1s clock
            // Timer's text updates from being the one thing still moving on
            // an otherwise-static black screen.
            //
            // Standby takes it away for that same reason and one more: a
            // clock is the one thing on screen that looks like proof the
            // device is working normally, which is precisely the wrong
            // impression to leave next to "please re-enable the device".
            visible: app.track.sessionActive && !app.standby
        }

        NowPlayingView {
            id: nowPlaying
            Layout.fillWidth: true
            Layout.fillHeight: true
            showTrack: window.showTrack
            previousLine: poller.previousLine
            currentLine: poller.currentLine
            nextLine: poller.nextLine
            creditsMode: poller.creditsMode
        }

        PlaybackBar {
            Layout.fillWidth: true
            Layout.preferredHeight: implicitHeight
            position: poller.position
            fraction: poller.fraction
        }
    }
}
