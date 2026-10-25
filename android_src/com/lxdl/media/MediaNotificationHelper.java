package com.lxdl.media;

import android.Manifest;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import java.util.concurrent.ArrayBlockingQueue;

/** Native callbacks only enqueue strings. Python polls them on the Kivy thread.
 * Process-local only: deliberately no foreground service, wake lock or restart.
 * All session/receiver/notification mutations run on Android's main Looper.
 */
public final class MediaNotificationHelper {
    private static final String TAG = "LXMediaNotification";
    private static final String CHANNEL = "lx_playback_v1";
    private static final int NOTIFICATION_ID = 21814;
    private static final int PERMISSION_REQUEST = 21815;
    private final Context context;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ArrayBlockingQueue<String> commands = new ArrayBlockingQueue<String>(64);
    private final String actionPrefix;
    private volatile boolean closed;
    private volatile boolean permissionRequested;
    private NotificationManager manager;
    private MediaSession session;
    private BroadcastReceiver receiver;
    private boolean registered;
    private String notificationKey;
    private String playbackKey;
    private long lastPosition = -10000;

    public MediaNotificationHelper(Activity activity) {
        context = activity.getApplicationContext();
        actionPrefix = context.getPackageName() + ".lxmedia." + java.util.UUID.randomUUID().toString() + ".";
        handler.post(new Runnable() {
            @Override public void run() {
                if (closed) return;
                try { initialize(); }
                catch (Exception error) {
                    Log.w(TAG, "Initialization failed; playback unaffected", error);
                    release();
                }
            }
        });
    }

    private void initialize() {
        manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL, "音乐播放", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("落雪音乐播放控制");
            channel.setSound(null, null);
            channel.enableVibration(false);
            manager.createNotificationChannel(channel);
        }
        session = new MediaSession(context, "LXPlayback");
        session.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS);
        session.setCallback(new MediaSession.Callback() {
            @Override public void onPlay() { enqueue("play"); }
            @Override public void onPause() { enqueue("pause"); }
            @Override public void onStop() { enqueue("stop"); }
            @Override public void onSkipToPrevious() { enqueue("previous"); }
            @Override public void onSkipToNext() { enqueue("next"); }
            @Override public void onSeekTo(long position) {
                enqueue("seek:" + (Math.max(0, position) / 1000.0));
            }
        }, handler);
        receiver = new BroadcastReceiver() {
            @Override public void onReceive(Context ignored, Intent intent) {
                String action = intent.getAction();
                if ((actionPrefix + "previous").equals(action)) enqueue("previous");
                else if ((actionPrefix + "toggle").equals(action)) enqueue("toggle");
                else if ((actionPrefix + "next").equals(action)) enqueue("next");
            }
        };
        IntentFilter filter = new IntentFilter();
        filter.addAction(actionPrefix + "previous");
        filter.addAction(actionPrefix + "toggle");
        filter.addAction(actionPrefix + "next");
        if (Build.VERSION.SDK_INT >= 33) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            // Pre-33 has no NOT_EXPORTED flag. Per-instance UUID actions prevent
            // guessing; intents are package-scoped and immutable when supported.
            context.registerReceiver(receiver, filter, null, handler);
        }
        registered = true;
    }

    private void enqueue(String command) {
        if (!closed) commands.offer(command); // bounded, never block Android main thread
    }

    public String drainCommand() {
        if (closed) return null;
        return commands.poll();
    }

    private boolean permissionGranted() {
        return Build.VERSION.SDK_INT < 33 || context.checkSelfPermission(
                Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    public boolean requestPermission(final Activity activity) {
        if (closed) return false;
        if (permissionGranted()) return true;
        if (permissionRequested) return false;
        permissionRequested = true; // only one prompt per helper, never from update()
        activity.runOnUiThread(new Runnable() {
            @Override public void run() {
                if (closed || activity.isFinishing()) return;
                try {
                    if (Build.VERSION.SDK_INT >= 33 && !permissionGranted()) {
                        activity.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},
                                PERMISSION_REQUEST);
                    }
                } catch (Exception error) {
                    Log.w(TAG, "Permission request failed; playback unaffected", error);
                }
            }
        });
        return false;
    }

    private PendingIntent actionIntent(String command, int requestCode) {
        Intent intent = new Intent(actionPrefix + command).setPackage(context.getPackageName());
        return PendingIntent.getBroadcast(context, requestCode, intent, pendingFlags());
    }

    private int pendingFlags() {
        return PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
    }

    public void update(final String title, final String artist, final String album,
                       final String state, final long position, final long duration) {
        if (closed) return;
        handler.post(new Runnable() {
            @Override public void run() {
                if (closed || session == null) return;
                try { publish(title, artist, album, state, position, duration); }
                catch (Exception error) {
                    Log.w(TAG, "Notification update failed; playback unaffected", error);
                }
            }
        });
    }

    private void publish(String title, String artist, String album, String state,
                         long position, long duration) {
        boolean playing = "playing".equals(state);
        boolean active = playing || "paused".equals(state) || "buffering".equals(state);
        if (!active) {
            manager.cancel(NOTIFICATION_ID);
            session.setActive(false);
            session.setPlaybackState(new PlaybackState.Builder()
                    .setState(PlaybackState.STATE_STOPPED, 0, 0).build());
            commands.clear();
            notificationKey = null;
            playbackKey = null;
            return;
        }
        long actions = PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                | PlaybackState.ACTION_SKIP_TO_NEXT | PlaybackState.ACTION_STOP
                | PlaybackState.ACTION_SEEK_TO;
        int playbackState = playing ? PlaybackState.STATE_PLAYING
                : ("buffering".equals(state) ? PlaybackState.STATE_BUFFERING : PlaybackState.STATE_PAUSED);
        String key = title + "\u0000" + artist + "\u0000" + album + "\u0000" + duration + "\u0000" + state;
        if (!key.equals(playbackKey) || Math.abs(position - lastPosition) >= 1000) {
            session.setPlaybackState(new PlaybackState.Builder().setActions(actions)
                    .setState(playbackState, position, playing ? 1.0f : 0.0f).build());
            if (!key.equals(playbackKey)) {
                session.setMetadata(new MediaMetadata.Builder()
                        .putString(MediaMetadata.METADATA_KEY_TITLE, title)
                        .putString(MediaMetadata.METADATA_KEY_ARTIST, artist)
                        .putString(MediaMetadata.METADATA_KEY_ALBUM, album)
                        .putLong(MediaMetadata.METADATA_KEY_DURATION, duration).build());
            }
            playbackKey = key;
            lastPosition = position;
        }
        if (!session.isActive()) session.setActive(true);
        if (!permissionGranted()) {
            manager.cancel(NOTIFICATION_ID);
            notificationKey = null;
            return; // MediaSession and playback remain usable even if denied
        }
        if (key.equals(notificationKey)) return;
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(context, CHANNEL) : new Notification.Builder(context);
        Intent launch = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
        if (launch != null) {
            launch.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            builder.setContentIntent(PendingIntent.getActivity(context, 21819, launch, pendingFlags()));
        }
        builder.setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle(title).setContentText(artist).setSubText(album)
                .setShowWhen(false).setOnlyAlertOnce(true).setOngoing(playing)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setCategory(Notification.CATEGORY_TRANSPORT)
                .addAction(android.R.drawable.ic_media_previous, "上一首", actionIntent("previous", 21816))
                .addAction(playing ? android.R.drawable.ic_media_pause : android.R.drawable.ic_media_play,
                        playing ? "暂停" : "播放", actionIntent("toggle", 21817))
                .addAction(android.R.drawable.ic_media_next, "下一首", actionIntent("next", 21818))
                .setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken())
                        .setShowActionsInCompactView(0, 1, 2));
        manager.notify(NOTIFICATION_ID, builder.build());
        notificationKey = key;
    }

    public void close() {
        if (closed) return;
        closed = true;
        commands.clear();
        handler.post(new Runnable() {
            @Override public void run() { release(); }
        });
    }

    private void release() {
        if (manager != null) manager.cancel(NOTIFICATION_ID);
        if (registered) {
            try { context.unregisterReceiver(receiver); }
            catch (IllegalArgumentException ignored) { }
            registered = false;
        }
        if (session != null) {
            session.setActive(false);
            session.release();
            session = null;
        }
        commands.clear();
    }
}
