import { CandlestickChart, LineChart } from "echarts/charts";
import {
  AxisPointerComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GridComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import EChartsReactCore from "echarts-for-react/lib/core";
import type { EChartsReactProps } from "echarts-for-react/lib/types";
import { forwardRef } from "react";

echarts.use([
  LineChart,
  CandlestickChart,
  GridComponent,
  TooltipComponent,
  AxisPointerComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  MarkLineComponent,
  MarkPointComponent,
  CanvasRenderer
]);

const AppChart = forwardRef<EChartsReactCore, Omit<EChartsReactProps, "echarts">>((props, ref) => (
  <EChartsReactCore ref={ref} echarts={echarts} {...props} />
));

AppChart.displayName = "AppChart";

export default AppChart;
export type AppChartRef = EChartsReactCore;
