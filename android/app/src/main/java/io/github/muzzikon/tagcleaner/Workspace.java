package io.github.muzzikon.tagcleaner;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import com.chaquo.python.PyException;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

final class Workspace {
    static final String[] FIELDS = {"title", "artist", "album", "albumartist", "date", "tracknumber", "discnumber", "remixer"};
    static final String[] LABELS = {"Название", "Исполнитель", "Альбом", "Исполнитель альбома", "Дата", "Номер трека", "Номер диска", "Ремиксер"};
    static final String[] FILTERS = {"Все треки", "С обложкой", "Не обработаны", "Обложка не найдена", "Очередь обложек", "Ошибки"};

    static final class Row {
        final Documents.Track track;
        final String key;
        Documents.Tags tags;
        String error;
        boolean noCandidate;
        String search;
        long modified;

        Row(Documents.Track track) {
            this.track = track;
            key = Documents.identity(track.uri());
            modified = track.modified();
            search = track.name().toLowerCase(Locale.ROOT);
        }

        void update(Documents.Tags tags) {
            if (this.tags != null && !this.tags.signature().equals(tags.signature())) noCandidate = false;
            this.tags = new Documents.Tags(tags.fields(), tags.duration(), tags.hasCover(), tags.signature(), tags.modified(), null);
            modified = tags.modified();
            search = (track.name() + " " + tags.fields().optString("title") + " "
                    + tags.fields().optString("artist") + " " + tags.fields().optString("album")).toLowerCase(Locale.ROOT);
            if (tags.hasCover()) noCandidate = false;
        }
    }

    record Detail(Row row, Documents.Tags tags, boolean backup, String readFailure) {}
    record Queued(Documents.Artwork artwork, Bitmap preview) {}
    private record Opened(Detail detail, Bitmap queuedPreview) {}
    private record Saved(Documents.Save result, Detail refreshed) {}
    private record Restored(Detail detail, boolean datePreserved) {}
    private record Scan(Row row, Documents.Tags tags, String error) {}
    private record BatchRow(Row row, Documents.Artwork artwork) {}

    interface Task<T> { T run() throws Exception; }

    final List<Row> rows = new ArrayList<>();
    private final Map<String, Documents.Artwork> queue = new LinkedHashMap<>();
    private final Map<Uri, Row> byUri = new HashMap<>();
    private static final ExecutorService WORKER = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean cancelled = new AtomicBoolean();
    private final Documents files;
    private final CoverSearch catalog = new CoverSearch();
    private final SharedPreferences preferences;
    private final SharedPreferences coverVerdicts;

    Runnable listener;
    Uri folder;
    List<Uri> pickedFiles = new ArrayList<>();
    Uri reopen;
    Detail detail;
    Queued queued;
    Documents.BackupStats backupInfo;
    List<CoverSearch.Candidate> candidates = new ArrayList<>();
    JSONObject draft = new JSONObject();
    boolean busy;
    boolean canCancel;
    boolean dateWarning;
    int listRevision;
    int detailRevision;
    String message = "";
    String search = "";
    int filter;
    boolean showDetail;
    int listIndex;
    int listOffset;

    Workspace(Context context) {
        files = new Documents(context);
        preferences = context.getSharedPreferences("workspace", Context.MODE_PRIVATE);
        coverVerdicts = context.getSharedPreferences("cover_verdicts", Context.MODE_PRIVATE);
        String tree = preferences.getString("folder", "");
        if (!tree.isEmpty()) folder = Uri.parse(tree);
        try {
            var savedFiles = new JSONArray(preferences.getString("files", "[]"));
            for (int i = 0; i < savedFiles.length(); i++) pickedFiles.add(Uri.parse(savedFiles.getString(i)));
            var savedQueue = new JSONObject(preferences.getString("queue", "{}"));
            var keys = savedQueue.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                JSONObject item = savedQueue.getJSONObject(key);
                var image = files.queuedFile(item.getString("file"));
                if (image.isFile()) queue.put(key, new Documents.Artwork(image, item.getString("mime"),
                        item.getInt("width"), item.getInt("height"), item.getString("signature")));
            }
        } catch (JSONException | IllegalArgumentException error) {
            Log.w("TagCleaner", "Invalid saved workspace", error);
        }
    }

    void setSource(Uri folder, List<Uri> pickedFiles) {
        this.folder = folder;
        this.pickedFiles = pickedFiles;
        var items = new JSONArray();
        for (Uri uri : pickedFiles) items.put(uri.toString());
        preferences.edit().putString("folder", folder == null ? "" : folder.toString())
                .putString("files", items.toString()).apply();
        listIndex = 0;
        listOffset = 0;
        reopen = null;
        load();
    }

    void load() {
        if (busy || (folder == null && pickedFiles.isEmpty())) return;
        detail = null;
        queued = null;
        showDetail = false;
        rows.clear();
        byUri.clear();
        listRevision++;
        detailRevision++;
        Uri selectedFolder = folder;
        List<Uri> selectedFiles = pickedFiles;
        submit("Читаем папку…", true, () -> {
            List<Documents.Track> source = selectedFolder == null
                    ? files.fromFiles(selectedFiles, cancelled::get) : files.fromTree(selectedFolder, cancelled::get);
            var incoming = new ArrayList<Row>(source.size());
            for (Documents.Track track : source) incoming.add(new Row(track));
            ui.post(() -> {
                rows.addAll(incoming);
                for (Row row : incoming) byUri.put(row.track.uri(), row);
                listRevision++;
                notifyChange();
            });
            int done = 0;
            long lastUpdate = SystemClock.elapsedRealtime();
            var batch = new ArrayList<Scan>();
            for (Row row : incoming) {
                if (cancelled.get()) break;
                try {
                    batch.add(new Scan(row, files.inspect(row.track, false), null));
                } catch (Exception error) {
                    batch.add(new Scan(row, null, errorText(error)));
                }
                done++;
                long now = SystemClock.elapsedRealtime();
                if (now - lastUpdate >= 220 || batch.size() == 128) {
                    reportScan(batch, done, incoming.size());
                    lastUpdate = now;
                    batch = new ArrayList<>();
                }
            }
            if (!batch.isEmpty()) reportScan(batch, done, incoming.size());
            return done;
        }, done -> {
            message = cancelled.get() ? "Сканирование остановлено · проверено " + done + " из " + rows.size()
                    : rows.isEmpty() ? "Аудиофайлы поддерживаемых форматов не найдены."
                    : failures() == 0 ? "" : "Не удалось прочитать: " + failures();
            if (reopen != null) {
                Row row = byUri.get(reopen);
                reopen = null;
                if (row != null) open(row);
            }
        });
    }

    private void reportScan(List<Scan> batch, int done, int total) {
        ui.post(() -> {
            for (Scan item : batch) {
                item.row().error = item.error();
                if (item.tags() != null) {
                    item.row().update(item.tags());
                    loadVerdict(item.row(), item.tags());
                }
            }
            listRevision++;
            message = "Читаем теги · " + done + " из " + total;
            notifyChange();
        });
    }

    void open(Row row) {
        if (busy) return;
        if (detail != null && detail.row() == row) {
            showDetail = true;
            notifyChange();
            return;
        }
        openFresh(row);
    }

    void refreshTrack() {
        if (detail != null && !busy) openFresh(detail.row());
    }

    private void openFresh(Row row) {
        showDetail = true;
        detail = null;
        queued = null;
        candidates = new ArrayList<>();
        detailRevision++;
        Documents.Artwork cover = cover(row);
        submit("Открываем трек…", false, () -> {
            Detail data = readDetail(row);
            Bitmap preview = cover == null ? null : Documents.preview(cover.file(), 512);
            return new Opened(data, preview);
        }, result -> {
            detail = result.detail();
            if (cover != null) queued = new Queued(cover, result.queuedPreview());
            setDraftFromTags();
            message = "";
        });
    }

    private Detail readDetail(Row row) throws Exception {
        boolean backup = files.hasBackup(row.track.uri());
        try {
            return new Detail(row, files.inspect(row.track, true), backup, null);
        } catch (Exception error) {
            String failure = errorText(error);
            String signature = backup ? files.signature(row.track) : "";
            return new Detail(row, new Documents.Tags(new JSONObject(), 0, false, signature, row.modified, null), backup, failure);
        }
    }

    private void setDraftFromTags() {
        if (detail.readFailure() == null) {
            long previous = detail.row().modified;
            detail.row().update(detail.tags());
            if (previous != detail.row().modified) reorder();
            loadVerdict(detail.row(), detail.tags());
        }
        if (detail.readFailure() != null || cover(detail.row()) == null) detail.row().error = detail.readFailure();
        draft = fieldsCopy(detail.tags().fields());
        detailRevision++;
        listRevision++;
    }

    private void loadVerdict(Row row, Documents.Tags tags) {
        if (!tags.hasCover() && tags.signature().equals(coverVerdicts.getString(row.key, ""))) row.noCandidate = true;
    }

    private void reorder() {
        rows.sort(Comparator.comparingLong((Row row) -> row.modified).reversed()
                .thenComparing(row -> row.track.name(), String.CASE_INSENSITIVE_ORDER));
    }

    void clean() {
        JSONObject snapshot = fieldsCopy(draft);
        submit("Проверяем текст тегов…", false, () -> files.clean(snapshot), result -> {
            boolean changed = !result.toString().equals(snapshot.toString());
            draft = result;
            message = changed ? "Черновик очищен. Проверьте поля и сохраните теги." : "Теги готовы к записи без очистки.";
            detailRevision++;
        });
    }

    void save(Uri export, boolean includeCover, boolean allowNewDate) {
        if (detail == null || detail.readFailure() != null || busy) return;
        Detail selected = detail;
        JSONObject updates = fieldsCopy(draft);
        Documents.Artwork cover = includeCover ? cover(selected.row()) : null;
        if (cover != null && !cover.signature().equals(selected.tags().signature())) {
            setMessage("Файл изменился. Выберите обложку заново перед сохранением копии.");
            return;
        }
        submit(export == null ? "Сохраняем теги…" : "Сохраняем копию…", false, () -> {
            Documents.Save result = files.edit(selected.row().track, selected.tags().signature(), updates, cover, export, allowNewDate);
            boolean updated = result == Documents.Save.UPDATED || result == Documents.Save.UPDATED_WITH_NEW_DATE;
            return new Saved(result, updated ? readDetail(selected.row()) : null);
        }, saved -> {
            if (saved.refreshed() != null) {
                advanceQueue(selected.row(), selected.tags().signature(), saved.refreshed().tags().signature());
                detail = saved.refreshed();
                setDraftFromTags();
            }
            dateWarning = saved.result() == Documents.Save.DATE_BLOCKED;
            message = switch (saved.result()) {
                case UNCHANGED -> "В тегах нет изменений.";
                case UPDATED -> "Теги сохранены. Дата изменения файла сохранена.";
                case COPIED -> "Копия сохранена с исходной датой.";
                case COPIED_WITH_NEW_DATE -> "Копия сохранена. Хранилище присвоило ей новую дату.";
                case DATE_BLOCKED -> "Хранилище не разрешает восстановить дату файла.";
                case UPDATED_WITH_NEW_DATE -> "Теги сохранены. Дата изменения файла обновилась.";
            };
        });
    }

    void showBackups() {
        submit("Проверяем резервные копии…", false, files::backupStats, result -> {
            backupInfo = result;
            message = "";
        });
    }

    void deleteBackups() {
        submit("Удаляем резервные копии…", false, () -> {
            files.deleteBackups();
            return null;
        }, unused -> {
            if (detail != null) detail = new Detail(detail.row(), detail.tags(), false, detail.readFailure());
            detailRevision++;
            message = "Резервные копии удалены.";
        });
    }

    void restore() {
        if (detail == null || busy) return;
        Detail selected = detail;
        submit("Восстанавливаем оригинал…", false, () -> {
            boolean date = files.restore(selected.row().track, selected.tags().signature());
            return new Restored(readDetail(selected.row()), date);
        }, restored -> {
            removeQueue(selected.row());
            detail = restored.detail();
            setDraftFromTags();
            message = restored.datePreserved() ? "Оригинал восстановлен."
                    : "Оригинал восстановлен. Хранилище обновило дату файла.";
        });
    }

    void searchCovers() {
        if (detail == null || busy) return;
        Row row = detail.row();
        String artist = draft.optString("artist").trim();
        String title = draft.optString("title").trim();
        String album = draft.optString("album").trim();
        double duration = detail.tags().duration();
        if (artist.isEmpty() || title.isEmpty() || duration <= 0) {
            setMessage("Для поиска укажите исполнителя и название трека. Нужна известная длительность.");
            return;
        }
        candidates = new ArrayList<>();
        detailRevision++;
        submit("Ищем обложки в MusicBrainz…", true,
                () -> catalog.find(artist, title, album, duration, cancelled::get), result -> {
                    candidates = result;
                    if (!cancelled.get()) {
                        row.noCandidate = result.isEmpty();
                        SharedPreferences.Editor update = coverVerdicts.edit().remove(row.key);
                        JSONObject saved = detail.tags().fields();
                        if (row.noCandidate && !detail.tags().hasCover() && artist.equals(saved.optString("artist").trim())
                                && title.equals(saved.optString("title").trim()) && album.equals(saved.optString("album").trim())) {
                            update.putString(row.key, detail.tags().signature());
                        }
                        update.apply();
                        message = result.isEmpty() ? "Для этого трека подходящие обложки не найдены."
                                : "Проверьте издание и выберите обложку.";
                    } else {
                        message = "Поиск остановлен.";
                    }
                    listRevision++;
                    detailRevision++;
                });
    }

    void chooseCover(Uri image, CoverSearch.Candidate candidate) {
        if (detail == null || busy) return;
        Detail selected = detail;
        String signature = selected.tags().signature();
        submit("Подготавливаем обложку…", false, () -> {
            Documents.Artwork artwork = candidate == null ? files.queue(image, signature)
                    : files.queue(catalog.artwork(candidate), signature);
            return new Queued(artwork, Documents.preview(artwork.file(), 512));
        }, item -> {
            Documents.Artwork previous = queue.put(key(selected.row()), item.artwork());
            saveQueue();
            if (previous != null) previous.file().delete();
            queued = item;
            selected.row().noCandidate = false;
            coverVerdicts.edit().remove(selected.row().key).apply();
            listRevision++;
            detailRevision++;
            message = "Обложка добавлена в очередь.";
        });
    }

    private void advanceQueue(Row row, String oldSignature, String newSignature) {
        Documents.Artwork old = cover(row);
        if (old == null || !old.signature().equals(oldSignature)) return;
        var next = new Documents.Artwork(old.file(), old.mime(), old.width(), old.height(), newSignature);
        queue.put(key(row), next);
        if (queued != null) queued = new Queued(next, queued.preview());
        saveQueue();
    }

    void removeQueue(Row row) {
        Documents.Artwork removed = queue.remove(key(row));
        if (removed == null) return;
        saveQueue();
        removed.file().delete();
        if (detail != null && detail.row() == row) queued = null;
        listRevision++;
        detailRevision++;
        notifyChange();
    }

    int queueSize() {
        if (queue.isEmpty()) return 0;
        int count = 0;
        for (Row row : rows) if (cover(row) != null) count++;
        return count;
    }

    void applyQueue(Row only, boolean allowNewDate) {
        var batch = new ArrayList<BatchRow>();
        for (Row row : rows) {
            Documents.Artwork cover = cover(row);
            if (cover != null && (only == null || only == row)) batch.add(new BatchRow(row, cover));
        }
        int total = batch.size();
        if (total == 0 || busy) return;
        Detail selected = detail;
        submit("Вшиваем обложки…", total > 1, () -> {
            int done = 0;
            int errors = 0;
            int newDates = 0;
            for (BatchRow item : batch) {
                if (cancelled.get()) break;
                Row row = item.row();
                try {
                    Documents.Save result = files.edit(row.track, item.artwork().signature(), new JSONObject(),
                            item.artwork(), null, allowNewDate);
                    if (result == Documents.Save.DATE_BLOCKED) {
                        throw new IllegalStateException("Хранилище запрещает восстановить дату. Разрешите новую дату или сохраните копию трека.");
                    }
                    if (result == Documents.Save.UPDATED_WITH_NEW_DATE) newDates++;
                    boolean isOpen = selected != null && selected.row() == row;
                    Documents.Tags tags = files.inspect(row.track, isOpen);
                    boolean backup = isOpen && files.hasBackup(row.track.uri());
                    ui.post(() -> {
                        long previous = row.modified;
                        row.update(tags);
                        if (previous != row.modified) reorder();
                        row.error = null;
                        if (isOpen) detail = new Detail(row, tags, backup, null);
                        removeQueue(row);
                    });
                } catch (Exception error) {
                    String failure = errorText(error);
                    ui.post(() -> {
                        row.error = failure;
                        listRevision++;
                        detailRevision++;
                    });
                    errors++;
                }
                int progress = ++done;
                ui.post(() -> setMessage("Вшиваем обложки · " + progress + " из " + total));
            }
            return new int[]{done, errors, newDates};
        }, result -> message = (cancelled.get() ? "Остановлено · " : "Готово · ")
                + (result[0] - result[1]) + " из " + total + (result[1] == 0 ? "" : " · ошибок: " + result[1])
                + (result[2] == 0 ? "" : " · с новой датой: " + result[2]));
    }

    private void saveQueue() {
        var data = new JSONObject();
        for (var entry : queue.entrySet()) {
            Documents.Artwork value = entry.getValue();
            var item = new JSONObject();
            put(item, "file", value.file().getName());
            put(item, "mime", value.mime());
            put(item, "width", value.width());
            put(item, "height", value.height());
            put(item, "signature", value.signature());
            put(data, entry.getKey(), item);
        }
        preferences.edit().putString("queue", data.toString()).apply();
    }

    Documents.Artwork cover(Row row) { return queue.get(key(row)); }
    private static String key(Row row) { return row.key; }

    List<Row> filtered() {
        String query = search.trim().toLowerCase(Locale.ROOT);
        var result = new ArrayList<Row>();
        for (Row row : rows) {
            if (!row.search.contains(query)) continue;
            boolean hasCover = row.tags != null && row.tags.hasCover();
            boolean include = switch (filter) {
                case 1 -> hasCover;
                case 2 -> !hasCover && !row.noCandidate && row.error == null && cover(row) == null;
                case 3 -> row.noCandidate && !hasCover;
                case 4 -> cover(row) != null;
                case 5 -> row.error != null;
                default -> true;
            };
            if (include) result.add(row);
        }
        return result;
    }

    private int failures() {
        int failures = 0;
        for (Row row : rows) if (row.error != null) failures++;
        return failures;
    }

    void cancel() {
        cancelled.set(true);
        setMessage("Останавливаем после текущего файла или запроса…");
    }

    void close() {
        cancelled.set(true);
        listener = null;
    }

    private <T> void submit(String progress, boolean canCancel, Task<T> task, Consumer<T> success) {
        if (busy) return;
        busy = true;
        this.canCancel = canCancel;
        cancelled.set(false);
        setMessage(progress);
        WORKER.execute(() -> {
            try {
                T result = task.run();
                ui.post(() -> {
                    busy = false;
                    success.accept(result);
                    notifyChange();
                });
            } catch (Exception error) {
                String failure = errorText(error);
                ui.post(() -> {
                    busy = false;
                    setMessage(failure);
                });
            }
        });
    }

    void setMessage(String text) {
        message = text;
        notifyChange();
    }

    void notifyChange() { if (listener != null) listener.run(); }

    static JSONObject fieldsCopy(JSONObject source) {
        var result = new JSONObject();
        for (String field : FIELDS) put(result, field, source.optString(field));
        return result;
    }

    static void put(JSONObject target, String key, Object value) {
        try {
            target.put(key, value);
        } catch (JSONException impossible) {
            throw new IllegalArgumentException(impossible);
        }
    }

    private static String errorText(Exception error) {
        Log.w("TagCleaner", "Operation failed", error);
        String message = error.getMessage();
        if (error instanceof PyException && message != null) message = message.replaceFirst("^[\\w.]+: ", "");
        return message == null || message.isEmpty() ? "Операция прервана. Повторите попытку." : message;
    }
}
