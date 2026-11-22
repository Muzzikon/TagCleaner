package io.github.muzzikon.tagcleaner;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Bitmap;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.Editable;
import android.text.InputType;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.text.format.Formatter;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.inputmethod.InputMethodManager;
import android.webkit.MimeTypeMap;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.PopupMenu;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

import java.text.DateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.function.Consumer;

public final class MainActivity extends Activity {
    private static final int FOLDER = 1, FILES = 2, ARTWORK = 3, EXPORT = 4;
    private static final int MATCH = ViewGroup.LayoutParams.MATCH_PARENT;
    private static final int WRAP = ViewGroup.LayoutParams.WRAP_CONTENT;
    private static final int SPLIT_WIDTH_DP = 720;

    private Workspace state;
    private LinearLayout root, body, library, inspector, detailContent, statusBar;
    private FrameLayout details;
    private ScrollView detailScroll;
    private ListView trackList;
    private TrackAdapter adapter;
    private TextView heading, statusText, emptyText;
    private Button menu, back, stop, section, queueAction;
    private ProgressBar progress;
    private final List<View> detailActions = new ArrayList<>();
    private int seenList = -1, seenDetail = -1, builtTab = -1, tab;
    private Workspace.Row renderedRow;
    private boolean split, backRegistered;
    private String shownQuery;
    private int shownFilter = -1;
    private OnBackInvokedCallback backCallback;

    @Override
    public void onCreate(Bundle saved) {
        super.onCreate(saved);
        state = (Workspace) getLastNonConfigurationInstance();
        boolean fresh = state == null;
        if (fresh) state = new Workspace(getApplicationContext());
        if (saved != null) {
            tab = saved.getInt("tab");
            if (fresh) {
                state.search = saved.getString("search", "");
                state.filter = saved.getInt("filter");
                state.listIndex = saved.getInt("position");
                state.listOffset = saved.getInt("offset");
                String selected = saved.getString("selected");
                if (selected != null) state.reopen = Uri.parse(selected);
            }
        }
        build();
        if (fresh) state.load();
    }

    @Override
    protected void onStart() {
        super.onStart();
        state.listener = this::render;
        render();
    }

    @Override
    protected void onStop() {
        rememberListPosition();
        state.listener = null;
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        if (backRegistered && Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(backCallback);
        }
        if (isFinishing()) state.close();
        super.onDestroy();
    }

    @Override
    public Object onRetainNonConfigurationInstance() {
        rememberListPosition();
        return state;
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        rememberListPosition();
        out.putInt("tab", tab);
        out.putInt("filter", state.filter);
        out.putInt("position", state.listIndex);
        out.putInt("offset", state.listOffset);
        out.putString("search", state.search);
        if (state.showDetail && state.detail != null) out.putString("selected", state.detail.row().track.uri().toString());
        super.onSaveInstanceState(out);
    }

    private void build() {
        root = column();
        root.setBackgroundColor(color(R.color.background));
        if (Build.VERSION.SDK_INT >= 35) {
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                var padding = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                        | WindowInsets.Type.ime());
                view.setPadding(padding.left, padding.top, padding.right, padding.bottom);
                return insets;
            });
        }
        setContentView(root);
        root.requestApplyInsets();

        LinearLayout toolbar = line();
        toolbar.setBackgroundColor(color(R.color.surface));
        toolbar.setPadding(dp(12), 0, dp(8), 0);
        toolbar.setMinimumHeight(dp(56));
        back = button("‹", false, this::showLibrary);
        back.setContentDescription("К списку треков");
        back.setTextSize(26);
        toolbar.addView(back, new LinearLayout.LayoutParams(dp(48), WRAP));
        var mark = new ImageView(this);
        mark.setImageResource(R.drawable.ic_tagcleaner);
        mark.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        toolbar.addView(mark, new LinearLayout.LayoutParams(dp(28), dp(28)));
        heading = label("TagCleaner", 20, R.color.text, true);
        heading.setPadding(dp(10), 0, 0, 0);
        if (Build.VERSION.SDK_INT >= 28) heading.setAccessibilityHeading(true);
        toolbar.addView(heading, new LinearLayout.LayoutParams(0, WRAP, 1));
        menu = button("⋮", false, this::openMenu);
        menu.setContentDescription("Действия с библиотекой");
        menu.setTextSize(24);
        toolbar.addView(menu, new LinearLayout.LayoutParams(dp(48), WRAP));
        root.addView(toolbar, new LinearLayout.LayoutParams(MATCH, WRAP));
        root.addView(divider(), new LinearLayout.LayoutParams(MATCH, dp(1)));

        body = line();
        body.setGravity(Gravity.TOP);
        library = buildLibrary();
        details = new FrameLayout(this);
        details.setBackgroundColor(color(R.color.surface));
        inspector = column();
        details.addView(inspector, new FrameLayout.LayoutParams(MATCH, MATCH));
        buildInspector();
        body.addView(library);
        body.addView(details);
        root.addView(body, new LinearLayout.LayoutParams(MATCH, 0, 1));

        statusBar = line();
        statusBar.setPadding(dp(12), dp(4), dp(8), dp(4));
        statusBar.setBackgroundColor(color(R.color.surface));
        progress = new ProgressBar(this);
        LinearLayout.LayoutParams progressSize = new LinearLayout.LayoutParams(dp(20), dp(20));
        progressSize.setMarginEnd(dp(10));
        statusBar.addView(progress, progressSize);
        statusText = label("", 13, R.color.muted, false);
        statusText.setMaxLines(2);
        statusText.setEllipsize(TextUtils.TruncateAt.END);
        statusText.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        statusText.setOnClickListener(view -> new AlertDialog.Builder(this).setMessage(state.message)
                .setPositiveButton("Закрыть", null).show());
        statusBar.addView(statusText, new LinearLayout.LayoutParams(0, WRAP, 1));
        stop = button("Стоп", false, state::cancel);
        statusBar.addView(stop, new LinearLayout.LayoutParams(WRAP, WRAP));
        root.addView(statusBar, new LinearLayout.LayoutParams(MATCH, WRAP));

        body.addOnLayoutChangeListener((view, left, top, right, bottom, oldLeft, oldTop, oldRight, oldBottom) -> {
            boolean wide = right - left >= dp(SPLIT_WIDTH_DP);
            if (wide != split) {
                split = wide;
                setPanes();
            }
        });
        setPanes();
    }

    private LinearLayout buildLibrary() {
        LinearLayout panel = column();
        LinearLayout filters = line();
        filters.setPadding(dp(12), dp(8), dp(12), dp(8));
        var query = new EditText(this);
        query.setHint("Поиск треков");
        query.setTextSize(15);
        query.setTextColor(color(R.color.text));
        query.setHintTextColor(color(R.color.muted));
        query.setInputType(InputType.TYPE_CLASS_TEXT);
        query.setSingleLine(true);
        query.setMinHeight(dp(48));
        query.setPadding(dp(12), 0, dp(12), 0);
        query.setBackground(shape(R.color.surface, R.color.outline, 10));
        query.setText(state.search);
        watch(query, value -> {
            state.search = value;
            state.listIndex = 0;
            state.listOffset = 0;
            render();
        });
        var querySize = new LinearLayout.LayoutParams(0, WRAP, 1);
        querySize.setMarginEnd(dp(8));
        filters.addView(query, querySize);
        section = button("Разделы", false, this::chooseSection);
        section.setMaxWidth(dp(152));
        filters.addView(section, new LinearLayout.LayoutParams(WRAP, WRAP));
        panel.addView(filters, new LinearLayout.LayoutParams(MATCH, WRAP));
        queueAction = button("Вшить обложки", true, () -> confirmBatch(null));
        add(panel, queueAction, 0, 6, 12);

        var stack = new FrameLayout(this);
        trackList = new ListView(this);
        trackList.setDivider(shape(R.color.outline, 0, 0));
        trackList.setDividerHeight(dp(1));
        trackList.setPadding(dp(10), 0, dp(10), 0);
        trackList.setClipToPadding(false);
        trackList.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);
        adapter = new TrackAdapter();
        trackList.setAdapter(adapter);
        trackList.setOnItemClickListener((parent, view, position, id) -> {
            if (state.busy) return;
            Workspace.Row row = adapter.getItem(position);
            if (state.detail != null && state.detail.row() == row) state.open(row);
            else confirmDiscard(() -> {
                if (state.filter >= 1 && state.filter <= 4) tab = 1;
                state.open(row);
            });
        });
        stack.addView(trackList, new FrameLayout.LayoutParams(MATCH, MATCH));

        var empty = new ScrollView(this);
        empty.setFillViewport(true);
        LinearLayout actions = column();
        actions.setGravity(Gravity.CENTER);
        actions.setPadding(dp(20), dp(14), dp(20), dp(14));
        emptyText = label("Выберите папку или файлы с музыкой", 17, R.color.muted, false);
        emptyText.setGravity(Gravity.CENTER);
        add(actions, emptyText, 0, 12, 0);
        add(actions, button("Выбрать папку", true, () -> chooseAudio(FOLDER)), 0, 8, 0);
        add(actions, button("Выбрать файлы", false, () -> chooseAudio(FILES)), 0, 0, 0);
        empty.addView(actions, new ScrollView.LayoutParams(MATCH, WRAP));
        stack.addView(empty, new FrameLayout.LayoutParams(MATCH, MATCH));
        trackList.setEmptyView(empty);
        panel.addView(stack, new LinearLayout.LayoutParams(MATCH, 0, 1));
        return panel;
    }

    private void buildInspector() {
        detailScroll = new ScrollView(this);
        detailScroll.setFillViewport(true);
        detailScroll.setClipToPadding(false);
        detailContent = column();
        detailContent.setPadding(dp(16), dp(14), dp(16), dp(16));
        detailContent.setFocusableInTouchMode(true);
        detailScroll.addView(detailContent, new ScrollView.LayoutParams(MATCH, WRAP));
        inspector.addView(detailScroll, new LinearLayout.LayoutParams(MATCH, 0, 1));
    }

    private void setPanes() {
        if (trackList.getVisibility() == View.VISIBLE && library.getVisibility() == View.VISIBLE) rememberListPosition();
        library.setLayoutParams(new LinearLayout.LayoutParams(split ? 0 : MATCH, MATCH, split ? 1f : 0));
        details.setLayoutParams(new LinearLayout.LayoutParams(split ? 0 : MATCH, MATCH, split ? 1.08f : 0));
        library.setVisibility(split || !state.showDetail ? View.VISIBLE : View.GONE);
        details.setVisibility(split || state.showDetail ? View.VISIBLE : View.GONE);
        back.setVisibility(!split && state.showDetail ? View.VISIBLE : View.GONE);
        back.setEnabled(true);
        heading.setText(state.showDetail && !split ? "Трек" : "TagCleaner");
        if (Build.VERSION.SDK_INT >= 33) {
            boolean shouldRegister = !split && state.showDetail;
            if (shouldRegister && !backRegistered) {
                backCallback = this::showLibrary;
                getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                        OnBackInvokedDispatcher.PRIORITY_DEFAULT, backCallback);
            } else if (!shouldRegister && backRegistered) {
                getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(backCallback);
            }
            backRegistered = shouldRegister;
        }
    }

    private void render() {
        boolean listVisible = split || !state.showDetail;
        if ((library.getVisibility() == View.VISIBLE) != listVisible) setPanes();
        boolean sectionChanged = shownFilter != state.filter || !state.search.equals(shownQuery);
        if (seenList != state.listRevision || sectionChanged) {
            if (!sectionChanged) rememberListPosition();
            seenList = state.listRevision;
            shownFilter = state.filter;
            shownQuery = state.search;
            adapter.rows = state.filtered();
            adapter.notifyDataSetChanged();
            if (!adapter.rows.isEmpty()) trackList.setSelectionFromTop(
                    Math.min(state.listIndex, adapter.rows.size() - 1), state.listOffset);
            section.setContentDescription("Разделы. " + Workspace.FILTERS[state.filter]);
            section.setText(state.filter == 0 ? "Разделы" : Workspace.FILTERS[state.filter]);
            emptyText.setText(state.rows.isEmpty() ? "Выберите папку или файлы с музыкой" : "В этом разделе нет треков по вашему запросу");
        }
        if (seenDetail != state.detailRevision || builtTab != tab) renderDetail();
        menu.setEnabled(!state.busy);
        trackList.setEnabled(!state.busy);
        for (View action : detailActions) action.setEnabled(!state.busy);
        stateMessage();
        int queued = state.queueSize();
        queueAction.setVisibility(state.filter == 4 && queued > 0 ? View.VISIBLE : View.GONE);
        queueAction.setEnabled(!state.busy);
        queueAction.setText(getString(R.string.apply_queued, queued));
        if (state.backupInfo != null) showBackupDialog();
        if (state.dateWarning) showDateWarning();
    }

    private void stateMessage() {
        statusBar.setVisibility(state.message.isEmpty() ? View.GONE : View.VISIBLE);
        progress.setVisibility(state.busy ? View.VISIBLE : View.GONE);
        stop.setVisibility(state.busy && state.canCancel ? View.VISIBLE : View.GONE);
        if (!statusText.getText().toString().equals(state.message)) statusText.setText(state.message);
    }

    private void renderDetail() {
        seenDetail = state.detailRevision;
        Workspace.Detail data = state.detail;
        Workspace.Row row = data == null ? null : data.row();
        int previousScroll = row == renderedRow && tab == builtTab ? detailScroll.getScrollY() : 0;
        renderedRow = row;
        builtTab = tab;
        detailActions.clear();
        detailContent.removeAllViews();
        if (data == null) {
            add(detailContent, label("Выберите трек в библиотеке", 17, R.color.muted, false), 12, 0, 0);
            return;
        }

        String title = data.tags().fields().optString("title");
        TextView trackTitle = label(title.isEmpty() ? row.track.name() : title, 23, R.color.text, true);
        if (Build.VERSION.SDK_INT >= 28) trackTitle.setAccessibilityHeading(true);
        add(detailContent, trackTitle, 0, 3, 0);
        String date = row.modified > 0 ? " · " + DateFormat.getDateTimeInstance(DateFormat.SHORT,
                DateFormat.SHORT).format(new Date(row.modified)) : "";
        add(detailContent, label(row.track.name() + date, 13, R.color.muted, false), 0, 12, 0);
        if (data.readFailure() != null) {
            add(detailContent, label(data.readFailure(), 15, R.color.accent, false), 0, 12, 0);
            showBackupAction(data);
            action("Открыть заново", false, state::refreshTrack);
            return;
        }
        LinearLayout tabs = line();
        Button tagTab = button("Теги", tab == 0, () -> setTab(0));
        Button coverTab = button("Обложка", tab == 1, () -> setTab(1));
        tagTab.setSelected(tab == 0);
        coverTab.setSelected(tab == 1);
        LinearLayout.LayoutParams tabSize = new LinearLayout.LayoutParams(0, WRAP, 1);
        tabSize.setMarginEnd(dp(8));
        tabs.addView(tagTab, tabSize);
        tabs.addView(coverTab, new LinearLayout.LayoutParams(0, WRAP, 1));
        add(detailContent, tabs, 0, 14, 0);
        if (row.error != null) add(detailContent, label(row.error, 14, R.color.accent, false), 0, 10, 0);
        if (tab == 0) showTags(data);
        else showCover(data);
        detailScroll.post(() -> detailScroll.scrollTo(0, previousScroll));
    }

    private void showTags(Workspace.Detail data) {
        for (int index = 0; index < Workspace.FIELDS.length; index++) {
            String field = Workspace.FIELDS[index];
            var editor = new EditText(this);
            editor.setId(View.generateViewId());
            TextView fieldLabel = label(Workspace.LABELS[index], 13, R.color.muted, true);
            fieldLabel.setLabelFor(editor.getId());
            add(detailContent, fieldLabel, 0, 4, 0);
            editor.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
            editor.setTextSize(16);
            editor.setTextColor(color(R.color.text));
            editor.setMinHeight(dp(48));
            editor.setMaxLines(4);
            editor.setPadding(dp(12), dp(8), dp(12), dp(8));
            editor.setBackground(shape(R.color.background, R.color.outline, 9));
            editor.setText(state.draft.optString(field));
            watch(editor, value -> Workspace.put(state.draft, field, value));
            detailActions.add(editor);
            add(detailContent, editor, 0, 11, 0);
        }
        action("Очистить текст тегов", false, state::clean);
        action("Заполнить из имени файла", false, this::fillFromFilename);
        if (data.row().track.writable()) action("Сохранить теги", true, () -> {
            hideKeyboard();
            state.save(null, false, false);
        });
        action("Сохранить копию", !data.row().track.writable(), this::export);
        showBackupAction(data);
        action("Открыть заново", false, () -> confirmDiscard(state::refreshTrack));
    }

    private void showBackupAction(Workspace.Detail data) {
        if (data.backup() && data.row().track.writable()) {
            action("Восстановить оригинал", false, () -> confirm("Восстановить оригинал?",
                    "Последняя резервная копия заменит текущие теги и обложку. Если хранилище запрещает восстановить дату, файл получит новую.",
                    "Восстановить", state::restore));
        }
    }

    private void fillFromFilename() {
        String name = state.detail.row().track.name();
        String[] parts = name.substring(0, name.lastIndexOf('.')).split("\\s+[-–—]\\s+", 2);
        if (parts.length != 2 || parts[0].trim().isEmpty() || parts[1].trim().isEmpty()) {
            state.setMessage("Нужно имя файла в формате «Исполнитель - Название».");
            return;
        }
        Workspace.put(state.draft, "artist", parts[0].trim());
        Workspace.put(state.draft, "title", parts[1].trim());
        state.detailRevision++;
        state.setMessage("Проверьте исполнителя и название в черновике.");
    }

    private void showCover(Workspace.Detail data) {
        add(detailContent, label(data.tags().hasCover() ? "Встроенная обложка" : "Встроенной обложки нет", 16,
                R.color.text, true), 0, 10, 0);
        image(data.tags().preview(), "Встроенная обложка", 148);
        action("Выбрать JPEG или PNG", !data.tags().hasCover(), this::pickImage);
        action("Найти обложку", false, state::searchCovers);
        action("Открыть заново", false, () -> confirmDiscard(state::refreshTrack));
        if (state.queued != null) {
            add(detailContent, label("В очереди", 17, R.color.accent, true), 12, 8, 0);
            image(state.queued.preview(), "Обложка в очереди", 148);
            boolean stale = !state.queued.artwork().signature().equals(data.tags().signature());
            if (stale) {
                add(detailContent, label("Файл изменился. Выберите обложку заново.", 14, R.color.muted, false), 0, 8, 0);
            } else if (data.row().track.writable()) {
                action("Вшить обложку", true, () -> confirmBatch(data.row()));
            } else {
                action("Сохранить копию с обложкой", true, this::export);
            }
            action("Убрать из очереди", false, () -> state.removeQueue(data.row()));
        }
        if (!state.candidates.isEmpty()) {
            add(detailContent, label("Варианты из MusicBrainz", 17, R.color.text, true), 14, 8, 0);
            for (CoverSearch.Candidate candidate : state.candidates) {
                CoverSearch.Edition edition = candidate.edition();
                String release = edition.name() + (edition.date().isEmpty() ? "" : " · " + edition.date())
                        + (edition.country().isEmpty() ? "" : " · " + edition.country());
                add(detailContent, label(release, 15, R.color.text, false), 8, 8, 0);
                image(candidate.preview(), "Обложка издания «" + edition.name() + "»", 116);
                action("В очередь", false, () -> confirmReplacement(() -> state.chooseCover(null, candidate)));
            }
        }
    }

    private void action(String text, boolean primary, Runnable handler) {
        Button button = button(text, primary, handler);
        detailActions.add(button);
        add(detailContent, button, 0, 7, 0);
    }

    private void image(Bitmap bitmap, String description, int size) {
        if (bitmap == null) return;
        var view = new ImageView(this);
        view.setImageBitmap(bitmap);
        view.setContentDescription(description);
        view.setScaleType(ImageView.ScaleType.FIT_CENTER);
        view.setBackground(shape(R.color.background, R.color.outline, 10));
        view.setClipToOutline(true);
        var params = new LinearLayout.LayoutParams(dp(size), dp(size));
        params.bottomMargin = dp(12);
        detailContent.addView(view, params);
    }

    private void setTab(int tab) {
        hideKeyboard();
        this.tab = tab;
        render();
    }

    private void chooseSection() {
        new AlertDialog.Builder(this).setTitle("Разделы библиотеки")
                .setSingleChoiceItems(Workspace.FILTERS, state.filter, (dialog, selected) -> {
                    state.filter = selected;
                    state.listIndex = 0;
                    state.listOffset = 0;
                    dialog.dismiss();
                    render();
                }).setNegativeButton("Отмена", null).show();
    }

    private void openMenu() {
        var popup = new PopupMenu(this, menu);
        var entries = popup.getMenu();
        entries.add(0, FOLDER, 0, "Выбрать папку");
        entries.add(0, FILES, 1, "Выбрать файлы");
        if (state.folder != null || !state.pickedFiles.isEmpty()) entries.add(0, 5, 2, "Пересканировать библиотеку");
        if (state.queueSize() > 0) entries.add(0, 6, 3, "Вшить обложки из очереди");
        entries.add(0, 7, 4, "Резервные копии");
        popup.setOnMenuItemClickListener(item -> {
            switch (item.getItemId()) {
                case FOLDER, FILES -> chooseAudio(item.getItemId());
                case 5 -> confirmDiscard(state::load);
                case 6 -> confirmBatch(null);
                case 7 -> state.showBackups();
                default -> { return false; }
            }
            return true;
        });
        popup.show();
    }

    private void confirmBatch(Workspace.Row row) {
        String message = row == null ? "Вшить выбранные обложки в треки текущей библиотеки?"
                : "Вшить выбранную обложку в трек «" + row.track.name() + "»?";
        var date = new CheckBox(this);
        date.setText("Разрешить новую дату, если хранилище запрещает сохранить исходную");
        date.setMinHeight(dp(48));
        var content = column();
        content.setPadding(dp(20), 0, dp(20), 0);
        content.addView(date);
        new AlertDialog.Builder(this).setTitle("Вшить обложки?").setMessage(message).setView(content)
                .setNegativeButton("Отмена", null)
                .setPositiveButton("Вшить", (dialog, button) -> state.applyQueue(row, date.isChecked())).show();
    }

    private void showDateWarning() {
        state.dateWarning = false;
        new AlertDialog.Builder(this).setTitle("Хранилище не позволяет сохранить дату")
                .setMessage("Можно сохранить отдельную копию или записать теги в исходный трек с новой датой изменения.")
                .setNegativeButton("Отмена", null)
                .setNeutralButton("Копия", (dialog, button) -> export())
                .setPositiveButton("С новой датой", (dialog, button) -> state.save(null, false, true)).show();
    }

    private void confirmDiscard(Runnable action) {
        if (state.detail == null || state.detail.readFailure() != null
                || state.detail.tags().fields().toString().equals(state.draft.toString())) action.run();
        else confirm("Закрыть черновик?", "Несохранённые правки тегов будут сброшены.", "Продолжить", action);
    }

    private void confirmReplacement(Runnable action) {
        if (state.detail != null && state.detail.tags().hasCover()) {
            confirm("Заменить встроенную обложку?", "Новая картинка будет добавлена в очередь.", "В очередь", action);
        } else action.run();
    }

    private void confirm(String title, String message, String positive, Runnable action) {
        new AlertDialog.Builder(this).setTitle(title).setMessage(message).setNegativeButton("Отмена", null)
                .setPositiveButton(positive, (dialog, button) -> action.run()).show();
    }

    private void showBackupDialog() {
        Documents.BackupStats stats = state.backupInfo;
        state.backupInfo = null;
        String size = Formatter.formatFileSize(this, stats.bytes());
        var dialog = new AlertDialog.Builder(this).setTitle("Резервные копии")
                .setMessage("Оригиналов: " + stats.count() + " · " + size)
                .setNegativeButton("Закрыть", null);
        if (stats.count() != 0) dialog.setPositiveButton("Удалить все", (view, which) -> confirm(
                "Удалить резервные копии?", "Восстановить прежние теги и обложки из этих копий больше не получится.",
                "Удалить", state::deleteBackups));
        dialog.show();
    }

    private void chooseAudio(int request) {
        if (state.busy) return;
        confirmDiscard(() -> {
            Intent intent;
            if (request == FOLDER) {
                intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
            } else {
                intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE)
                        .putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"audio/*", "video/mp4", "application/ogg", "application/octet-stream"})
                        .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
            }
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                    | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
            launchPicker(intent, request);
        });
    }

    private void pickImage() {
        launchPicker(new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("image/*").addCategory(Intent.CATEGORY_OPENABLE), ARTWORK);
    }

    private void export() {
        if (state.detail == null) return;
        if (state.queued != null && !state.queued.artwork().signature().equals(state.detail.tags().signature())) {
            state.setMessage("Выберите обложку заново или уберите её из очереди перед сохранением копии.");
            return;
        }
        String name = state.detail.row().track.name();
        int dot = name.lastIndexOf('.');
        String filename = dot < 0 ? name + " (копия)" : name.substring(0, dot) + " (копия)" + name.substring(dot);
        String mime = dot < 0 ? null : MimeTypeMap.getSingleton().getMimeTypeFromExtension(
                name.substring(dot + 1).toLowerCase(Locale.ROOT));
        launchPicker(new Intent(Intent.ACTION_CREATE_DOCUMENT).setType(mime == null ? "application/octet-stream" : mime)
                .addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_TITLE, filename), EXPORT);
    }

    private void launchPicker(Intent intent, int request) {
        hideKeyboard();
        try {
            startActivityForResult(intent, request);
        } catch (ActivityNotFoundException error) {
            state.setMessage("Системное приложение для выбора файлов недоступно.");
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (result != RESULT_OK || data == null) return;
        Uri uri = data.getData();
        if ((request == ARTWORK || request == EXPORT) && state.detail == null) {
            state.setMessage("Откройте трек повторно и продолжите операцию.");
            return;
        }
        if (request == ARTWORK && uri != null) {
            confirmReplacement(() -> state.chooseCover(uri, null));
        } else if (request == EXPORT && uri != null) {
            state.save(uri, state.queued != null, false);
        } else if (request == FOLDER && uri != null) {
            persist(uri, data.getFlags());
            state.setSource(uri, new ArrayList<>());
        } else if (request == FILES) {
            var selected = new ArrayList<Uri>();
            if (data.getClipData() != null) {
                for (int i = 0; i < data.getClipData().getItemCount(); i++) {
                    Uri item = data.getClipData().getItemAt(i).getUri();
                    if (item != null) selected.add(item);
                }
            } else if (uri != null) selected.add(uri);
            for (Uri item : selected) persist(item, data.getFlags());
            if (!selected.isEmpty()) state.setSource(null, selected);
        }
    }

    private void persist(Uri uri, int flags) {
        try {
            getContentResolver().takePersistableUriPermission(uri, flags & (Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION));
        } catch (SecurityException ignored) {
            // Keep using the session grant if the provider doesn't offer persistence.
        }
    }

    private void showLibrary() {
        hideKeyboard();
        state.showDetail = false;
        render();
    }

    @Override
    @SuppressLint("GestureBackNavigation") // API 33+ uses OnBackInvokedDispatcher above.
    public void onBackPressed() {
        if (Build.VERSION.SDK_INT < 33 && !split && state.showDetail) showLibrary();
        else super.onBackPressed();
    }

    private void rememberListPosition() {
        if (library != null && library.isShown() && trackList.getChildCount() > 0) {
            state.listIndex = trackList.getFirstVisiblePosition();
            state.listOffset = trackList.getChildAt(0).getTop() - trackList.getPaddingTop();
        }
    }

    private void hideKeyboard() {
        getSystemService(InputMethodManager.class).hideSoftInputFromWindow(root.getWindowToken(), 0);
    }

    private LinearLayout column() {
        var layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        return layout;
    }

    private LinearLayout line() {
        var layout = new LinearLayout(this);
        layout.setGravity(Gravity.CENTER_VERTICAL);
        return layout;
    }

    private void add(LinearLayout parent, View child, int top, int bottom, int sides) {
        var params = new LinearLayout.LayoutParams(MATCH, WRAP);
        params.setMargins(dp(sides), dp(top), dp(sides), dp(bottom));
        parent.addView(child, params);
    }

    private View divider() {
        var view = new View(this);
        view.setBackgroundColor(color(R.color.outline));
        return view;
    }

    private TextView label(String text, int size, int color, boolean prominent) {
        var view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(color(color));
        if (prominent) view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        return view;
    }

    private Button button(String text, boolean primary, Runnable onClick) {
        var view = new Button(this);
        view.setAllCaps(false);
        view.setText(text);
        view.setTextSize(14);
        view.setMinWidth(0);
        view.setMinHeight(dp(48));
        view.setMinimumHeight(dp(48));
        view.setPadding(dp(10), dp(4), dp(10), dp(4));
        int[][] states = {{-android.R.attr.state_enabled}, {}};
        view.setTextColor(new ColorStateList(states,
                new int[]{color(R.color.muted), color(primary ? R.color.accent_text : R.color.text)}));
        view.setBackground(new RippleDrawable(ColorStateList.valueOf(0x33ffffff), shape(
                primary ? R.color.accent : R.color.surface, primary ? 0 : R.color.outline, 9), null));
        view.setStateListAnimator(null);
        view.setOnClickListener(clicked -> onClick.run());
        return view;
    }

    private GradientDrawable shape(int fill, int outline, int radius) {
        var background = new GradientDrawable();
        background.setColor(color(fill));
        background.setCornerRadius(dp(radius));
        if (outline != 0) background.setStroke(dp(1), color(outline));
        return background;
    }

    private static void watch(EditText view, Consumer<String> callback) {
        view.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) { callback.accept(s.toString()); }
            @Override public void afterTextChanged(Editable s) {}
        });
    }

    private int color(int resource) { return getColor(resource); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private final class TrackAdapter extends BaseAdapter {
        List<Workspace.Row> rows = new ArrayList<>();

        @Override public int getCount() { return rows.size(); }
        @Override public Workspace.Row getItem(int position) { return rows.get(position); }
        @Override public long getItemId(int position) { return position; }

        @Override
        public View getView(int position, View recycled, ViewGroup parent) {
            TrackTile tile = recycled instanceof TrackTile old ? old : new TrackTile();
            tile.bind(getItem(position));
            return tile;
        }
    }

    private final class TrackTile extends LinearLayout {
        private final TextView title, subtitle, coverStatus;

        TrackTile() {
            super(MainActivity.this);
            setOrientation(VERTICAL);
            setPadding(dp(12), dp(10), dp(12), dp(10));
            setMinimumHeight(dp(64));
            title = label("", 16, R.color.text, true);
            title.setMaxLines(2);
            title.setEllipsize(TextUtils.TruncateAt.END);
            subtitle = label("", 13, R.color.muted, false);
            subtitle.setMaxLines(1);
            subtitle.setEllipsize(TextUtils.TruncateAt.END);
            coverStatus = label("", 12, R.color.accent, false);
            addView(title, new LinearLayout.LayoutParams(MATCH, WRAP));
            addView(subtitle, new LinearLayout.LayoutParams(MATCH, WRAP));
            addView(coverStatus, new LinearLayout.LayoutParams(MATCH, WRAP));
        }

        void bind(Workspace.Row row) {
            String titleText = row.tags == null ? "" : row.tags.fields().optString("title");
            String artist = row.tags == null ? "" : row.tags.fields().optString("artist");
            String extension = row.track.name().substring(row.track.name().lastIndexOf('.') + 1).toUpperCase(Locale.ROOT);
            title.setText(titleText.isEmpty() ? row.track.name() : titleText);
            subtitle.setText(artist.isEmpty() ? extension : artist + " · " + extension);
            coverStatus.setText(row.error != null ? "Ошибка" : state.cover(row) != null ? "В очереди"
                    : row.tags == null ? "Ожидает проверки" : row.tags.hasCover() ? "С обложкой"
                    : row.noCandidate ? "Обложка не найдена" : "Без обложки");
            setBackgroundColor(state.detail != null && state.detail.row() == row ? color(R.color.surface)
                    : color(R.color.background));
        }
    }
}
