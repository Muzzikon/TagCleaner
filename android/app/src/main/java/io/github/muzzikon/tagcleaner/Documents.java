package io.github.muzzikon.tagcleaner;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsContract;
import android.provider.DocumentsContract.Document;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.function.BooleanSupplier;

final class Documents {
    static final int IMAGE_LIMIT = 16 * 1024 * 1024;
    private static final String AUDIO_EXTENSIONS = "|mp3|flac|m4a|mp4|ogg|opus|wav|aiff|aif|";
    private static final String[] FILE_COLUMNS = {
            Document.COLUMN_DISPLAY_NAME, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS
    };
    private static final String[] TREE_COLUMNS = {
            Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE,
            Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS
    };

    record Track(Uri uri, String name, long modified, boolean writable) {}
    record Tags(JSONObject fields, double duration, boolean hasCover, String signature, long modified, Bitmap preview) {}
    record Artwork(File file, String mime, int width, int height, String signature) {}
    record BackupStats(int count, long bytes) {}
    enum Save { UNCHANGED, UPDATED, COPIED, COPIED_WITH_NEW_DATE, DATE_BLOCKED, UPDATED_WITH_NEW_DATE }

    private final Context context;
    private final ContentResolver resolver;
    private final File workspace;
    private final File backups;
    private final File artwork;
    private PyObject python;

    Documents(Context context) {
        this.context = context;
        resolver = context.getContentResolver();
        workspace = new File(context.getCacheDir(), "audio");
        backups = new File(context.getFilesDir(), "originals");
        artwork = new File(context.getFilesDir(), "queue");
    }

    private PyObject python() throws IOException {
        if (python == null) {
            // This cache can contain staging files left by a terminated process.
            makeDirectory(workspace);
            removeTemporaryFiles(workspace);
            File[] abandoned = backups.listFiles(file -> file.isDirectory() && file.getName().startsWith("staging-"));
            if (abandoned != null) {
                for (File temporary : abandoned) {
                    removeTemporaryFiles(temporary);
                    temporary.delete();
                }
            }
            if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
            python = Python.getInstance().getModule("mobile_tags");
        }
        return python;
    }

    List<Track> fromTree(Uri tree, BooleanSupplier cancelled) throws IOException {
        var tracks = new ArrayList<Track>();
        var visited = new HashSet<String>();
        var pending = new ArrayDeque<String>();
        String root = DocumentsContract.getTreeDocumentId(tree);
        pending.add(root);
        visited.add(root);
        while (!pending.isEmpty() && !cancelled.getAsBoolean()) {
            Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, pending.remove());
            try (Cursor cursor = resolver.query(children, TREE_COLUMNS, null, null, null)) {
                if (cursor == null) throw new IOException("Не удалось прочитать папку.");
                while (!cancelled.getAsBoolean() && cursor.moveToNext()) {
                    String id = cursor.getString(0);
                    if (id == null || !visited.add(id)) continue;
                    if (Document.MIME_TYPE_DIR.equals(cursor.getString(2))) {
                        pending.add(id);
                    } else {
                        String name = cursor.getString(1);
                        if (supported(name)) {
                            tracks.add(track(DocumentsContract.buildDocumentUriUsingTree(tree, id),
                                    name, cursor.getLong(3), cursor.getInt(4)));
                        }
                    }
                }
            }
        }
        return sorted(tracks);
    }

    List<Track> fromFiles(List<Uri> files, BooleanSupplier cancelled) {
        var tracks = new ArrayList<Track>();
        Set<Uri> seen = new HashSet<>();
        for (Uri uri : files) {
            if (cancelled.getAsBoolean()) break;
            if (!seen.add(uri)) continue;
            try (Cursor cursor = resolver.query(uri, FILE_COLUMNS, null, null, null)) {
                if (cursor != null && cursor.moveToFirst() && supported(cursor.getString(0))) {
                    tracks.add(track(uri, cursor.getString(0), cursor.getLong(1), cursor.getInt(2)));
                }
            } catch (SecurityException ignored) {
                // A persisted grant can be revoked independently for each file.
            }
        }
        return sorted(tracks);
    }

    private static List<Track> sorted(List<Track> tracks) {
        tracks.sort(Comparator.comparingLong(Track::modified).reversed()
                .thenComparing(Track::name, String.CASE_INSENSITIVE_ORDER));
        return tracks;
    }

    private static Track track(Uri uri, String name, long modified, int flags) {
        return new Track(uri, name, modified, (flags & Document.FLAG_SUPPORTS_WRITE) != 0);
    }

    static boolean supported(String name) {
        if (name == null) return false;
        int dot = name.lastIndexOf('.');
        return dot >= 0 && AUDIO_EXTENSIONS.contains("|" + name.substring(dot + 1).toLowerCase(Locale.ROOT) + "|");
    }

    Tags inspect(Track track, boolean preview) throws Exception {
        PyObject engine = python();
        File image = preview ? File.createTempFile("preview-", ".image", workspace) : null;
        try (ParcelFileDescriptor descriptor = open(track.uri(), "r")) {
            var data = new JSONObject(engine.callAttr("inspect", descriptor.getFd(), track.name(),
                    image == null ? "" : image.getPath()).toString());
            Bitmap bitmap = image == null || image.length() == 0 ? null : preview(image, 512);
            return new Tags(data.getJSONObject("tags"), data.getDouble("duration"),
                    data.getBoolean("cover"), data.getString("signature"), data.getLong("modified"), bitmap);
        } finally {
            if (image != null) image.delete();
        }
    }

    JSONObject clean(JSONObject fields) throws Exception {
        return new JSONObject(python().callAttr("cleanup", fields.toString()).toString());
    }

    Artwork queue(Uri uri, String signature) throws IOException {
        try (InputStream source = resolver.openInputStream(uri)) {
            return queue(readLimited(source, IMAGE_LIMIT), signature);
        }
    }

    Artwork queue(byte[] image, String signature) throws IOException {
        BitmapFactory.Options options = imageBounds(image);
        if ((!"image/jpeg".equals(options.outMimeType) && !"image/png".equals(options.outMimeType))
                || options.outWidth <= 0 || options.outHeight <= 0
                || (long) options.outWidth * options.outHeight > 25_000_000) {
            throw new IOException("Выберите JPEG или PNG размером до 25 Мп и 16 МиБ.");
        }
        BitmapFactory.Options sampled = sample(options, 512);
        Bitmap decoded = BitmapFactory.decodeByteArray(image, 0, image.length, sampled);
        if (decoded == null) throw new IOException("Не удалось прочитать картинку.");
        decoded.recycle();
        makeDirectory(artwork);
        File file = File.createTempFile("cover-", ".image", artwork);
        try (var out = new FileOutputStream(file)) {
            out.write(image);
            out.getFD().sync();
        } catch (IOException error) {
            file.delete();
            throw error;
        }
        return new Artwork(file, options.outMimeType, options.outWidth, options.outHeight, signature);
    }

    File queuedFile(String name) {
        if (!name.matches("cover-[0-9]+\\.image")) throw new IllegalArgumentException("Неизвестная обложка.");
        return new File(artwork, name);
    }

    Save edit(Track track, String signature, JSONObject tags, Artwork cover, Uri export, boolean allowNewDate) throws Exception {
        if (export != null && identity(export).equals(identity(track.uri()))) {
            throw new IOException("Выберите другое имя для копии трека.");
        }
        PyObject engine = python();
        makeDirectory(backups);
        try (ParcelFileDescriptor input = open(track.uri(), "r");
             ParcelFileDescriptor output = open(export == null ? track.uri() : export, "rw")) {
            int result = engine.callAttr("edit", input.getFd(), track.name(), signature, tags.toString(),
                    cover == null ? "" : cover.file().getPath(), cover == null ? "" : cover.mime(),
                    cover == null ? 0 : cover.width(), cover == null ? 0 : cover.height(), workspace.getPath(),
                    backups.getPath(), identity(track.uri()), export == null ? -1 : output.getFd(),
                    export == null ? output.getFd() : -1, allowNewDate).toInt();
            return Save.values()[result];
        }
    }

    String signature(Track track) throws Exception {
        try (ParcelFileDescriptor descriptor = open(track.uri(), "r")) {
            return python().callAttr("describe", descriptor.getFd()).toString();
        }
    }

    boolean hasBackup(Uri uri) throws Exception {
        return new File(python().callAttr("backup_path", backups.getPath(), identity(uri)).toString()).isFile();
    }

    BackupStats backupStats() {
        File[] originals = backupFiles();
        long bytes = 0;
        for (File file : originals) bytes += file.length();
        return new BackupStats(originals.length, bytes);
    }

    void deleteBackups() throws IOException {
        int failed = 0;
        for (File file : backupFiles()) if (!file.delete()) failed++;
        if (failed != 0) throw new IOException("Не удалось удалить резервные копии: " + failed);
    }

    private File[] backupFiles() {
        File[] originals = backups.listFiles(file -> file.isFile() && file.getName().endsWith(".bak"));
        return originals == null ? new File[0] : originals;
    }

    boolean restore(Track track, String signature) throws Exception {
        try (ParcelFileDescriptor source = open(track.uri(), "r");
             ParcelFileDescriptor target = open(track.uri(), "rw")) {
            return python().callAttr("restore", source.getFd(), track.name(), signature,
                    workspace.getPath(), backups.getPath(), identity(track.uri()), target.getFd()).toBoolean();
        }
    }

    static String identity(Uri uri) {
        try {
            return uri.getAuthority() + "/" + DocumentsContract.getDocumentId(uri);
        } catch (IllegalArgumentException ignored) {
            return uri.toString();
        }
    }

    private ParcelFileDescriptor open(Uri uri, String mode) throws IOException {
        ParcelFileDescriptor descriptor = resolver.openFileDescriptor(uri, mode);
        if (descriptor == null) throw new IOException("Хранилище не открыло файл.");
        return descriptor;
    }

    static Bitmap preview(File image, int maxEdge) {
        var bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(image.getPath(), bounds);
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null;
        return BitmapFactory.decodeFile(image.getPath(), sample(bounds, maxEdge));
    }

    static Bitmap preview(byte[] image, int maxEdge) {
        var bounds = imageBounds(image);
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null;
        return BitmapFactory.decodeByteArray(image, 0, image.length, sample(bounds, maxEdge));
    }

    private static BitmapFactory.Options imageBounds(byte[] image) {
        var bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(image, 0, image.length, bounds);
        return bounds;
    }

    private static BitmapFactory.Options sample(BitmapFactory.Options bounds, int maxEdge) {
        var result = new BitmapFactory.Options();
        result.inSampleSize = 1;
        while (Math.max(bounds.outWidth, bounds.outHeight) / result.inSampleSize > maxEdge) {
            result.inSampleSize *= 2;
        }
        return result;
    }

    static byte[] readLimited(InputStream input, int limit) throws IOException {
        if (input == null) throw new IOException("Хранилище не открыло файл.");
        var output = new ByteArrayOutputStream();
        byte[] buffer = new byte[32 * 1024];
        int count;
        while ((count = input.read(buffer)) != -1) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedIOException();
            if (count > limit - output.size()) throw new IOException("Превышен допустимый размер файла.");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static void makeDirectory(File directory) throws IOException {
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("Нет доступа к рабочей папке.");
    }

    private static void removeTemporaryFiles(File directory) {
        File[] files = directory.listFiles();
        if (files == null) return;
        for (File file : files) {
            if (file.isDirectory()) removeTemporaryFiles(file);
            file.delete();
        }
    }
}
