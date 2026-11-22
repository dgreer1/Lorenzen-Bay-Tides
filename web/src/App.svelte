<script lang="ts">
  import { onMount } from "svelte";
  import { init, use } from "echarts/core";
  import { LineChart } from "echarts/charts";
  import {
    DataZoomComponent,
    GridComponent,
    MarkAreaComponent,
    MarkLineComponent,
    MarkPointComponent,
    TooltipComponent,
  } from "echarts/components";
  import type { ECharts, EChartsOption } from "echarts/core";
  import { CanvasRenderer } from "echarts/renderers";

  use([
    LineChart,
    DataZoomComponent,
    GridComponent,
    MarkAreaComponent,
    MarkLineComponent,
    MarkPointComponent,
    TooltipComponent,
    CanvasRenderer,
  ]);

  const timezone = "Pacific/Auckland";
  const baseUrl = import.meta.env.BASE_URL;

  interface Chunk {
    file: string;
    start_ms: number;
    count: number;
    end_ms: number;
  }

  interface Manifest {
    timezone: string;
    level_units: string;
    threshold_m: number;
    validation: {
      rmse_m: number;
    };
    model: {
      source_time_basis: string;
      time_basis_diagnostic: {
        metservice_reference: {
          events: number;
          mean_absolute_error_minutes: {
            UTC: { mean_absolute_error_minutes: number };
            NZST: { mean_absolute_error_minutes: number };
          };
        };
        selection_reason: string;
      };
      calibration: {
        time_local: string;
        predicted_level_m: number;
        falling_tide: boolean;
      };
    };
    forecast: {
      start_ms: number;
      end_ms: number;
      chunks: Chunk[];
    };
  }

  interface TideEvent {
    time: string;
    type: "high" | "low";
    level_m: number;
  }

  interface TideChunk {
    start_ms: number;
    step_ms: number;
    levels_m: number[];
  }

  interface Point {
    time: number;
    level: number;
  }

  interface SailWindow {
    start: number;
    end: number;
  }

  interface SailingSlot {
    start: number;
    high: number;
    end: number;
    durationMinutes: number;
  }

  interface SailingDay {
    key: string;
    label: string;
    slots: SailingSlot[];
  }

  interface TideReferenceLine {
    name: string;
    level: number;
    color: string;
  }

  let chartElement: HTMLDivElement;
  let chart: ECharts | undefined;
  let manifest: Manifest | undefined;
  let events: TideEvent[] = [];
  let selectedDay = todayInRaglan();
  let daysToShow = 3;
  let loading = true;
  let errorMessage = "";
  let visiblePoints: Point[] = [];
  let visibleEvents: TideEvent[] = [];
  let sailWindows: SailWindow[] = [];
  let sailingDays: SailingDay[] = [];
  let referenceLines: TideReferenceLine[] = [];
  let nowMs = Date.now();
  const chunkCache = new Map<string, Promise<TideChunk>>();

  function todayInRaglan(): string {
    return new Intl.DateTimeFormat("sv-SE", { timeZone: timezone }).format(new Date());
  }

  function localDateToUtc(dateText: string): number {
    const [year, month, day] = dateText.split("-").map(Number);
    const localAsUtc = Date.UTC(year, month - 1, day);
    let result = localAsUtc;
    const formatter = new Intl.DateTimeFormat("en-GB", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const parts = Object.fromEntries(
        formatter.formatToParts(new Date(result)).map((part) => [part.type, part.value]),
      );
      const representedAsUtc = Date.UTC(
        Number(parts.year),
        Number(parts.month) - 1,
        Number(parts.day),
        Number(parts.hour),
        Number(parts.minute),
        Number(parts.second),
      );
      result = localAsUtc - (representedAsUtc - result);
    }
    return result;
  }

  function addCalendarDays(dateText: string, count: number): string {
    const [year, month, day] = dateText.split("-").map(Number);
    const result = new Date(Date.UTC(year, month - 1, day + count));
    return `${result.getUTCFullYear()}-${String(result.getUTCMonth() + 1).padStart(2, "0")}-${String(result.getUTCDate()).padStart(2, "0")}`;
  }

  function localDateKey(value: number): string {
    return new Intl.DateTimeFormat("sv-SE", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(value));
  }

  function formatDateLabel(dateText: string): string {
    return new Intl.DateTimeFormat("en-NZ", {
      timeZone: timezone,
      weekday: "long",
      day: "numeric",
      month: "long",
    }).format(new Date(localDateToUtc(dateText)));
  }

  function formatTime(value: number | string, includeDate = false): string {
    const date = new Date(value);
    return new Intl.DateTimeFormat("en-NZ", {
      timeZone: timezone,
      ...(includeDate ? { weekday: "short", day: "numeric", month: "short" } : {}),
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    }).format(date);
  }

  function formatSlotTime(value: number, highDay: string): string {
    return localDateKey(value) === highDay ? formatTime(value) : formatTime(value, true);
  }

  function formatAxisTime(value: number): string {
    return new Intl.DateTimeFormat("en-NZ", {
      timeZone: timezone,
      ...(daysToShow > 1 ? { day: "numeric", month: "short" } : {}),
      hour: "numeric",
      hour12: true,
    }).format(new Date(value));
  }

  function getChunk(chunk: Chunk): Promise<TideChunk> {
    let request = chunkCache.get(chunk.file);
    if (!request) {
      request = fetch(`${baseUrl}data/${chunk.file}`).then((response) => {
        if (!response.ok) throw new Error(`Could not load forecast data (${response.status}).`);
        return response.json() as Promise<TideChunk>;
      });
      chunkCache.set(chunk.file, request);
    }
    return request;
  }

  async function loadRange(start: number, end: number): Promise<Point[]> {
    if (!manifest) return [];
    const chunks = manifest.forecast.chunks.filter(
      (chunk) => chunk.end_ms >= start && chunk.start_ms <= end,
    );
    const loaded = await Promise.all(chunks.map((chunk) => getChunk(chunk)));
    const points: Point[] = [];

    for (const data of loaded) {
      for (let index = 0; index < data.levels_m.length; index += 1) {
        const time = data.start_ms + index * data.step_ms;
        if (time >= start && time <= end) points.push({ time, level: data.levels_m[index] });
      }
    }
    return points;
  }

  function findWindows(points: Point[], threshold: number): SailWindow[] {
    const windows: SailWindow[] = [];
    let start = points[0]?.level >= threshold ? points[0].time : undefined;

    for (let index = 1; index < points.length; index += 1) {
      const previous = points[index - 1];
      const current = points[index];
      if (previous.level < threshold && current.level >= threshold) {
        const fraction = (threshold - previous.level) / (current.level - previous.level);
        start = previous.time + fraction * (current.time - previous.time);
      } else if (previous.level >= threshold && current.level < threshold && start !== undefined) {
        const fraction = (threshold - previous.level) / (current.level - previous.level);
        const end = previous.time + fraction * (current.time - previous.time);
        windows.push({ start, end });
        start = undefined;
      }
    }
    if (start !== undefined && points.length) {
      windows.push({ start, end: points[points.length - 1].time });
    }
    return windows;
  }

  function makeSailingDays(highTides: TideEvent[], windows: SailWindow[]): SailingDay[] {
    const days = new Map<string, SailingDay>();
    for (const event of highTides) {
      if (event.type !== "high") continue;
      const high = new Date(event.time).getTime();
      const window = windows.find((candidate) => candidate.start <= high && high <= candidate.end);
      if (!window) continue;

      const key = localDateKey(high);
      let day = days.get(key);
      if (!day) {
        const label = formatDateLabel(key);
        day = { key, label: key === todayInRaglan() ? `Today · ${label}` : label, slots: [] };
        days.set(key, day);
      }
      day.slots.push({
        start: window.start,
        high,
        end: window.end,
        durationMinutes: Math.round((window.end - window.start) / 60_000),
      });
    }
    return [...days.values()];
  }

  function formatDuration(minutes: number): string {
    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return hours ? `${hours}h ${remainingMinutes}m` : `${remainingMinutes}m`;
  }

  function getReferenceLines(start: number, days: number): TideReferenceLine[] {
    const referenceEnd = localDateToUtc(addCalendarDays(selectedDay, Math.max(days, 30)));
    const referenceEvents = events.filter((event) => {
      const eventTime = new Date(event.time).getTime();
      return eventTime >= start && eventTime <= referenceEnd;
    });
    const highs = referenceEvents
      .filter((event) => event.type === "high")
      .map((event) => event.level_m);
    const lows = referenceEvents
      .filter((event) => event.type === "low")
      .map((event) => event.level_m);
    if (!highs.length || !lows.length) return [];

    return [
      { name: "Spring high", level: Math.max(...highs), color: "#b86f36" },
      { name: "Neap high", level: Math.min(...highs), color: "#d19a43" },
      { name: "Neap low", level: Math.max(...lows), color: "#4b8c91" },
      { name: "Spring low", level: Math.min(...lows), color: "#35677d" },
    ];
  }

  function downsampleForChart(points: Point[], threshold: number): Point[] {
    if (points.length <= 1800) return points;

    const targetCount = Math.max(300, ((chart?.getWidth() ?? 0) - 80) * 2);
    const stride = Math.ceil(points.length / targetCount);
    const indices = new Set<number>([0, points.length - 1]);
    if (daysToShow <= 7 || (chart?.getWidth() ?? 0) > 700) {
      for (let index = 0; index < points.length; index += stride) indices.add(index);
    }

    for (let index = 1; index < points.length; index += 1) {
      const previous = points[index - 1].level;
      const current = points[index].level;
      if ((previous < threshold && current >= threshold) || (previous >= threshold && current < threshold)) {
        indices.add(index - 1);
        indices.add(index);
      }
    }

    for (const event of visibleEvents) {
      const index = Math.round((new Date(event.time).getTime() - points[0].time) / 60_000);
      if (index >= 0 && index < points.length) indices.add(index);
    }

    return [...indices]
      .sort((left, right) => left - right)
      .map((index) => points[index]);
  }

  function renderChart(): void {
    if (!chart || !manifest || !visiblePoints.length) return;
    const chartPoints = downsampleForChart(visiblePoints, manifest.threshold_m);
    const highLowPoints = (daysToShow <= 7 ? visibleEvents : []).map((event) => ({
      name: event.type === "high" ? "High" : "Low",
      coord: [new Date(event.time).getTime(), event.level_m],
      value: event.type === "high" ? "High" : "Low",
      itemStyle: { color: event.type === "high" ? "#f5a85d" : "#71c7cf" },
      label: { position: event.type === "high" ? "top" : "bottom" },
    }));
    const thresholdData = chartPoints.map((point) => [point.time, manifest.threshold_m]);
    const aboveThresholdData = chartPoints.map((point) => [
      point.time,
      Math.max(point.level - manifest.threshold_m, 0),
    ]);
    let minLevel = manifest.threshold_m;
    let maxLevel = manifest.threshold_m;
    for (const point of visiblePoints) {
      minLevel = Math.min(minLevel, point.level);
      maxLevel = Math.max(maxLevel, point.level);
    }
    for (const reference of referenceLines) {
      minLevel = Math.min(minLevel, reference.level);
      maxLevel = Math.max(maxLevel, reference.level);
    }
    const levelPadding = Math.max((maxLevel - minLevel) * 0.12, 0.12);

    const option: EChartsOption = {
      animation: false,
      grid: { left: 64, right: 24, top: 34, bottom: 82 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (items) => {
          const item = Array.isArray(items) ? items[0] : items;
          const value = Number(item?.value?.[1]);
          return `${formatTime(Number(item?.value?.[0]), true)}<br/>${value.toFixed(3)} m`;
        },
      },
      xAxis: {
        type: "time",
        min: visiblePoints[0].time,
        max: visiblePoints[visiblePoints.length - 1].time,
        axisLabel: {
          formatter: (value: number) => formatAxisTime(value),
          hideOverlap: true,
        },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        name: "Water level (m)",
        min: minLevel - levelPadding,
        max: maxLevel + levelPadding,
        splitNumber: 5,
        axisLabel: { formatter: (value: number) => value.toFixed(1) },
        splitLine: { lineStyle: { color: "#e5edf0" } },
      },
      dataZoom: [
        { type: "inside", xAxisIndex: 0, filterMode: "none" },
        { type: "slider", xAxisIndex: 0, height: 22, bottom: 24, brushSelect: false },
      ],
      series: [
        {
          name: "Predicted tide",
          type: "line",
          z: 3,
          showSymbol: false,
          data: chartPoints.map((point) => [point.time, point.level]),
          lineStyle: { color: "#087e8b", width: 2.5 },
          markLine: {
            symbol: ["none", "none"],
            data: [
              {
                yAxis: manifest.threshold_m,
                name: "Grounding level",
                lineStyle: { color: "#c35b3b", type: "dashed", width: 2 },
                label: {
                  formatter: `Float threshold ${manifest.threshold_m.toFixed(2)} m`,
                  position: "insideStartTop",
                },
              },
              {
                xAxis: nowMs,
                name: "Now",
                lineStyle: { color: "#273642", width: 2 },
                label: { formatter: "Now" },
              },
              ...referenceLines.map((reference) => ({
                yAxis: reference.level,
                name: reference.name,
                lineStyle: { color: reference.color, type: "dotted" as const, width: 1.5 },
                label: {
                  formatter: `${reference.name} ${reference.level.toFixed(2)} m`,
                  position: "insideStartTop" as const,
                },
              })),
            ],
          },
          markPoint: {
            symbolSize: 30,
            label: {
              color: "#18343d",
              fontSize: 9,
              fontWeight: 700,
              formatter: "{b}",
            },
            data: highLowPoints,
          },
        },
        {
          name: "Threshold baseline",
          type: "line",
          z: 1,
          stack: "above-threshold",
          showSymbol: false,
          data: thresholdData,
          lineStyle: { opacity: 0 },
          areaStyle: { opacity: 0 },
          silent: true,
          tooltip: { show: false },
        },
        {
          name: "Water above threshold",
          type: "line",
          z: 2,
          stack: "above-threshold",
          showSymbol: false,
          data: aboveThresholdData,
          lineStyle: { opacity: 0 },
          areaStyle: { color: "rgba(97, 159, 104, 0.30)" },
          silent: true,
          tooltip: { show: false },
        },
      ],
    };
    chart.setOption(option, true);
  }

  async function refreshChart(): Promise<void> {
    if (!manifest) return;
    loading = true;
    errorMessage = "";
    try {
      if (!selectedDay) throw new Error("Choose a start date.");
      daysToShow = Math.max(1, Math.min(30, Math.floor(Number(daysToShow) || 1)));
      const endDay = addCalendarDays(selectedDay, daysToShow);
      const start = localDateToUtc(selectedDay);
      const end = localDateToUtc(endDay);
      const windowStart = Math.max(manifest.forecast.start_ms, start - 86_400_000);
      const windowEnd = Math.min(manifest.forecast.end_ms, end + 86_400_000);
      const [chartPoints, windowPoints] = await Promise.all([
        loadRange(start, end),
        loadRange(windowStart, windowEnd),
      ]);
      visiblePoints = chartPoints;
      visibleEvents = events.filter((event) => {
        const eventTime = new Date(event.time).getTime();
        return eventTime >= start && eventTime <= end;
      });
      sailWindows = findWindows(windowPoints, manifest.threshold_m);
      sailingDays = makeSailingDays(visibleEvents, sailWindows);
      referenceLines = getReferenceLines(start, daysToShow);
      if (!visiblePoints.length) {
        errorMessage = "This date is outside the generated forecast. Choose a date within the next year.";
      }
      renderChart();
    } catch (error) {
      errorMessage = error instanceof Error ? error.message : "Could not load the tide forecast.";
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    chart = init(chartElement, undefined, { renderer: "canvas" });
    const resizeObserver = new ResizeObserver(() => chart?.resize());
    resizeObserver.observe(chartElement);
    const timer = window.setInterval(() => {
      nowMs = Date.now();
      renderChart();
    }, 30_000);

    void (async () => {
      try {
        const [manifestResponse, eventsResponse] = await Promise.all([
          fetch(`${baseUrl}data/manifest.json`),
          fetch(`${baseUrl}data/events.json`),
        ]);
        if (!manifestResponse.ok || !eventsResponse.ok) {
          throw new Error("The forecast data is not available yet. Try again shortly.");
        }
        manifest = (await manifestResponse.json()) as Manifest;
        const eventData = (await eventsResponse.json()) as { events: TideEvent[] };
        events = eventData.events;
        await refreshChart();
      } catch (error) {
        errorMessage = error instanceof Error ? error.message : "Could not initialize the tide viewer.";
      } finally {
        loading = false;
      }
    })();

    return () => {
      window.clearInterval(timer);
      resizeObserver.disconnect();
      chart?.dispose();
    };
  });
</script>

<svelte:head>
  <title>Lorenzen Bay Tide &amp; Mooring Planner</title>
  <meta name="description" content="Tide predictions and return windows for Lorenzen Bay, Raglan." />
</svelte:head>

<main class="page-shell">
  <header class="masthead">
    <div class="brand-mark" aria-hidden="true">LB</div>
    <div>
      <p class="eyebrow">Raglan Harbour · Aotearoa New Zealand</p>
      <h1>Lorenzen Bay <span>Tide planner</span></h1>
    </div>
    <div class="clock-card">
      <span>Raglan local time</span>
      <strong>{formatTime(nowMs, true)}</strong>
    </div>
  </header>

  {#if manifest}
    <section class="planner-card" aria-labelledby="schedule-title">
      <div class="planner-toolbar">
        <div>
          <p class="eyebrow">Mooring schedule</p>
          <h2 id="schedule-title">
            {#if daysToShow === 1 && selectedDay === todayInRaglan()}
              Today's sailing windows
            {:else}
              Sailing windows
            {/if}
          </h2>
          <p class="range-caption">
            {daysToShow} day{daysToShow === 1 ? "" : "s"} ·
            {formatDateLabel(selectedDay)} to
            {formatDateLabel(addCalendarDays(selectedDay, daysToShow - 1))}
          </p>
        </div>
        <div class="controls">
        <label>
          <span>Start date</span>
          <input type="date" bind:value={selectedDay} on:change={refreshChart} />
        </label>
        <label>
          <span>Number of days (1–30)</span>
          <input type="number" min="1" max="30" step="1" bind:value={daysToShow} on:change={refreshChart} />
        </label>
      </div>
      </div>

      {#if loading}
        <p class="schedule-message">Loading today's tide windows…</p>
      {:else if errorMessage}
        <p class="schedule-message error-message" role="alert">{errorMessage}</p>
      {:else if sailingDays.length}
        <div class="table-wrap">
          <table class="schedule-table">
            <thead>
              <tr>
                <th scope="col">Leave mooring</th>
                <th scope="col">High tide</th>
                <th scope="col">Back by</th>
                <th scope="col">Time off mooring</th>
              </tr>
            </thead>
            {#each sailingDays as day (day.key)}
              <tbody>
                <tr class="day-row">
                  <th colspan="4" scope="rowgroup">{day.label}</th>
                </tr>
                {#each day.slots as slot}
                  <tr class="sailing-row">
                    <td data-label="Leave mooring">{formatSlotTime(slot.start, day.key)}</td>
                    <td data-label="High tide">{formatSlotTime(slot.high, day.key)}</td>
                    <td data-label="Back by">{formatSlotTime(slot.end, day.key)}</td>
                    <td data-label="Time off mooring">{formatDuration(slot.durationMinutes)}</td>
                  </tr>
                {/each}
              </tbody>
            {/each}
          </table>
        </div>
      {:else}
        <p class="schedule-message">No above-threshold window contains a high tide in this date range.</p>
      {/if}
    </section>
  {/if}

  <section class="graph-card" aria-labelledby="chart-title">
    <div class="graph-heading">
      <div>
        <p class="eyebrow">One-minute harmonic prediction</p>
        <h2 id="chart-title">Tide level</h2>
        <p class="range-caption">Green shading is only the water above the float threshold. Neap and spring lines use the next 30 days of predictions.</p>
      </div>
    </div>
    <div class="legend" aria-label="Chart legend">
      <span><i class="legend-line"></i> Predicted tide</span>
      <span><i class="legend-window"></i> Above threshold</span>
      <span><i class="legend-threshold"></i> Float threshold</span>
      <span><i class="legend-reference"></i> Neap / spring levels</span>
      <span><i class="legend-now"></i> Current time</span>
    </div>
    <div class="chart-wrap">
      <div bind:this={chartElement} class="chart" role="img" aria-label="Interactive predicted tide chart"></div>
      {#if loading}<div class="loading-overlay">Loading tide predictions…</div>{/if}
      {#if errorMessage}<div class="message-overlay" role="alert">{errorMessage}</div>{/if}
    </div>
  </section>

  {#if manifest}
    <footer>
      <p>
        UTide predictions use the Raglan Wharf record and fixed NZST source timestamps, then display in
        Pacific/Auckland time. The estimated float threshold is {manifest.threshold_m.toFixed(2)} m,
        calibrated from the reported grounding at {formatTime(manifest.model.calibration.time_local, true)}.
        Levels are treated as metres; the source vertical datum is unspecified. The 45-day validation
        RMSE was {manifest.validation.rmse_m.toFixed(2)} m. MetService event-time mean error:
        {manifest.model.time_basis_diagnostic.metservice_reference.mean_absolute_error_minutes.UTC.mean_absolute_error_minutes.toFixed(1)} min (UTC) versus
        {manifest.model.time_basis_diagnostic.metservice_reference.mean_absolute_error_minutes.NZST.mean_absolute_error_minutes.toFixed(1)} min (NZST).
      </p>
    </footer>
  {/if}
</main>
