# coding: utf-8
###
 # @file   histogram.py
 # @author Sébastien Rouault <sebastien.rouault@alumni.epfl.ch>
 #
 # @section LICENSE
 #
 # Copyright © 2020-2021 École Polytechnique Fédérale de Lausanne (EPFL).
 # See LICENSE file.
 #
 # @section DESCRIPTION
 #
 # Plot the histogram of per-worker norm/variance estimations across the steps.
###

import tools

import aggregators
import experiments

import atexit
import json
import math
import matplotlib
import matplotlib.pyplot as plt
import pathlib
import pandas
import threading
import torch

# Change common font for the default LaTeX one
plt.rcParams["font.family"] = "Latin Modern Roman"
plt.rcParams["font.size"] = 16

# Enable automatic layout adjustments
plt.rcParams["figure.autolayout"] = True

# ---------------------------------------------------------------------------- #
# Common GTK main loop

try:
  import gi
  gi.require_version("Gtk", "3.0")
  from gi.repository import Gtk, Gdk, GLib
  print("Hi")

  gtk_lazy_lock = threading.Lock()
  gtk_lazy_main = None

  def gtk_run(closure):
    """ Run a closure in the GTK main loop, lazy start it.
    Args:
      closure Closure to run in the main loop
    """
    global gtk_lazy_lock
    global gtk_lazy_main
    # GTK's main event loop
    def gtk_main():
      # Main loop
      atexit.register(Gtk.main_quit)
      Gtk.main()
    # Lazy-start the loop if necessary
    with gtk_lazy_lock:
      if gtk_lazy_main is None:
        thread = threading.Thread(target=gtk_main, name="gtk_main", daemon=True)
        thread.start()
        gtk_lazy_main = thread
    # Submit the job to the main loop
    GLib.idle_add(closure)
except Exception as err:
  def gtk_run(closure):
    """ Sink in case GTK cannot be used.
    Args:
      closure Ignored parameter
    """
    tools.warning("GTK 3.0 is unavailable: %s" % (err,))

# ---------------------------------------------------------------------------- #
# Data frame columns selection helper

def select(data, *only_columns):
  """ "Intelligently" select columns from a data frame.
  Args:
    data Session or DataFrame to select
    ...  Only columns to select, empty for all
  Returns:
    (Sub-)dataframe, by reference
  """
  global Session
  # Unwrap data frame from session
  if isinstance(data, Session):
    data = data.data
  # Fast path
  if len(only_columns) == 0:
    return data
  # Intelligent selection
  columns = list()
  for only_column in only_columns:
    only_column = only_column.lower()
    for column in data.columns:
      if column not in columns and only_column in column.lower():
        columns.append(column)
  return data[columns]

def discard(data, *only_columns):
  """ "Intelligently" discard columns from a data frame.
  Args:
    ...  Only columns to discard, empty for none
  Returns:
    (Sub-)dataframe, by reference
  """
  # Fast path
  if len(only_columns) == 0:
    return data
  # Intelligent discarding
  data = data[:]
  for only_column in only_columns:
    only_column = only_column.lower()
    for column in data.columns:
      if only_column in column.lower():
        del data[column]
  return data

# ---------------------------------------------------------------------------- #
# DataFrame display (GTK-based)

class _DataFrameDisplayWindow(Gtk.Window):
  """ Display the given data frame in a window.
  """

  @staticmethod
  def to_string(x):
    """ Convert data to string, special treatment for floats.
    Args:
      x Input data
    Returns:
      Converted data to string
    """
    if type(x) is float:
      return "%e" % x
    return str(x).strip()

  def __init__(self, data, title="Display data"):
    """ Initialize the display window.
    Args:
      data  Data to display
      title Title to use
    """
    super().__init__(title=title)
    # Make and fill list store
    store = Gtk.ListStore(*([str] * (len(data.columns) + 1)))
    for row in data.itertuples():
      store.append(list(self.to_string(x) for x in row))
    # Make the associated tree view
    view = Gtk.TreeView(store)
    columns = list(data.columns)
    columns.insert(0, data.index.name)
    for i, cname in enumerate(columns):
      renderer = Gtk.CellRendererText()
      column = Gtk.TreeViewColumn(cname, renderer, text=i)
      view.append_column(column)
    # Make a scrolled window containing the tree view
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_hexpand(True)
    scrolled.set_vexpand(True)
    scrolled.add(view)
    self.add(scrolled)
    # Finalize window
    self.set_default_size(800, 600)

def display(data, **kwargs):
  """ GTK-based display of a data frame.
  Args:
    data Data frame to display
    ...  Forwarded keyword-arguments
  """
  # Display given data
  gtk_run(lambda: _DataFrameDisplayWindow(data, **kwargs).show_all())

# ---------------------------------------------------------------------------- #
# Training/evaluation data collection class

class Session:
  """ Training/evaluation data collection class.
  """

  def __init__(self, path_results):
    """ Load the data from a training/evaluation result directory.
    Args:
      path_results Path-like to the result directory to load
    """
    # Conversion to path
    if not isinstance(path_results, pathlib.Path):
      path_results = pathlib.Path(path_results)
    # Ensure directory exist
    if not path_results.exists():
      raise tools.UserException("Result directory %r cannot be accessed or does not exist" % str(path_results))
    # Load configuration string
    path_config = path_results / "config"
    try:
      data_config = path_config.read_text().strip()
    except Exception as err:
      tools.warning("Result directory %r: unable to read configuration (%s)" % (str(path_results), err))
      data_config = None
    # Load configuration json
    path_json = path_results / "config.json"
    try:
      with path_json.open("r") as fd:
        data_json = json.load(fd)
    except Exception as err:
      tools.warning("Result directory %r: unable to read JSON configuration (%s)" % (str(path_results), err))
      data_json = None
    # Load training data
    path_study = path_results / "study"
    try:
      data_study = pandas.read_csv(path_study, sep="\t", index_col=0, na_values="     nan")
      data_study.index.name = "Step number"
    except Exception as err:
      tools.warning("Result directory %r: unable to read training data (%s)" % (str(path_results), err))
      data_study = None
    # Load evaluation data
    path_eval = path_results / "eval"
    try:
      data_eval = pandas.read_csv(path_eval, sep="\t", index_col=0)
      data_eval.index.name = "Step number"
    except Exception as err:
      tools.warning("Result directory %r: unable to read evaluation data (%s)" % (str(path_results), err))
      data_eval = None
    # Merge data frames
    data = None
    for df in (data_study, data_eval):
      if df is None:
        continue
      if data is None:
        data = df
      else:
        data = data.join(df, how="outer")
    # Finalization
    self.name   = path_results.name
    self.path   = path_results
    self.config = data_config
    self.json   = data_json
    self.data   = data

  def get(self, *only_columns):
    """ Get (some of) the data.
    Args:
      name Name of the data frame to consider
      ...  Only columns to select, empty for all
    Returns:
      Selected data, by reference
    """
    global select
    return select(self.data, *only_columns)

  def display(self, *only_columns, name=None):
    """ Just display (some of) the data.
    Args:
      name Name of the data frame to consider
      ...  Only columns to select, empty for all
    Returns:
      self
    """
    global display
    # Display the (selected sub)set
    display(self.get(*only_columns), title=("Session data%s for %r" % (" (subset)" if len(only_columns) > 0 else "", self.name)))
    # Return self to enable chaining
    return self

  def has_known_ratio(self):
    """ Check whether the session's GAR has a known ratio.
    Returns:
      Whether the session's GAR has a known ratio
    """
    if self.json is None or "gar" not in self.json:
      tools.warning("No valid JSON-formatted configuration, cannot tell whether the associated GAR has a ratio")
      return False
    g = self.json["gar"]
    rule = aggregators.gars.get(g, None)
    return rule is not None and rule.upper_bound is not None

  def compute_all(self):
    """ Carries all the automated computations.
    Returns:
      self
    """
    # Carries all the computations
    for name, func in type(self).__dict__.items():
      if name == "compute_all":
        continue
      if name[:len("compute_")] == "compute_" and callable(func):
        func(self)
    # Return self to enable chaining
    return self

  def compute_epoch(self):
    """ Compute and append the epoch number, if not already done.
    Returns:
      self
    """
    column_name = "Epoch number"
    # Check if already there
    if column_name in self.data.columns:
      return self
    # Compute epoch number
    if self.json is None or "dataset" not in self.json:
      tools.warning("No valid JSON-formatted configuration, cannot compute the epoch number")
      return self
    dataset_name  = self.json["dataset"]
    training_size = {"mnist": 60000, "fashionmnist": 60000, "cifar10": 50000, "cifar100": 50000}.get(dataset_name, None)
    if training_size is None:
      tools.warning("Unknown dataset %r, cannot compute the epoch number" % dataset_name)
      return self
    self.data[column_name] = self.data["Training point count"] / training_size
    # Return self to enable chaining
    return self

  def compute_lr(self):
    """ Compute and append the learning rate, if not already done.
    Returns:
      self
    """
    column_name = "Learning rate"
    # Check if already there
    if column_name in self.data.columns:
      return self
    # Compute epoch number
    if self.json is None or "learning_rate" not in self.json:
      tools.warning("No valid JSON-formatted configuration, cannot compute the learning rate")
      return self
    lr = self.json["learning_rate"]
    lr_decay = self.json.get("learning_rate_decay", 0)
    lr_delta = self.json.get("learning_rate_decay_delta", 1)
    if lr_decay > 0:
      self.data[column_name] = lr / ((self.data.index // lr_delta * lr_delta) / lr_decay + 1)
    else:
      self.data[column_name] = lr
    # Return self to enable chaining
    return self

  # TODO: More automated computations of interest

# ---------------------------------------------------------------------------- #
# Plot management class

class LinePlot:
  """ Line plot management class.
  """

  # Known line styles
  linestyles = ("-", "--", ":", "-.")

  @classmethod
  def _get_line_style(self, ln):
    """ Get the line style and color for the given line number.
    Args:
      ln A non-negative integer representing the line number
    Returns:
      Associated line style, line color
    """
    return self.linestyles[ln % len(self.linestyles)], "C%d" % ln

  def __init__(self, index=None):
    """ Title constructor.
    Args:
      index Column name to use as the index instead of the default
    """
    # Make the subplots
    fig, ax = plt.subplots()
    # Store the non-finalized state
    self._fin = False # Not yet finalized
    self._fig = fig   # Figure instance
    self._ax  = ax    # Original axis instance
    self._tax = None  # Twin axis instance
    self._axs = {}    # Map column names to axis (up to two)
    self._idx = index # Column name to use as index by default, None to use dataframe's index
    self._cnt = 0     # Plot counter (to pick line style and color)

  def __del__(self):
    """ Close the figure on finalization.
    """
    self.close()

  def _get_ax(self, name):
    """ Get the axis associated with the column selector, make it if possible.
    Args:
      name Column selector
    Returns:
      Associated axis
    """
    # Return existing axis
    ax = self._axs.get(name, None)
    if ax is not None:
      return ax
    # Assert can make one more axis
    if len(self._axs) >= 2:
      raise RuntimeError("Line plot cannot have a 3rd y-axis")
    # Make one more axis
    if len(self._axs) == 0:
      ax = self._ax
    else:
      ax = self._ax.twinx()
      self._tax = ax
    self._axs[name] = ax
    # Return the axis
    return ax

  def include(self, data, *cols, errs=None, lalp=1., ccnt=None):
    """ Add the columns of the given data frame, can only be done before finalization.
    Args:
      data Session or dataframe holding the column(s) to add
      cols Column name(s) to include, mix selected columns together (same y-axis)
      errs Error suffix: for every selected column's real label, if a columns with 'real_label + errs' exists, it is used to display error bars
      lalp Line alpha level
      ccnt Color and linestyle number to use
    Returns:
      self
    """
    # Assert not already finalized
    if self._fin:
      raise RuntimeError("Plot is already finalized and cannot include another line")
    # Recover the dataframe if a session was given
    if isinstance(data, Session):
      data = data.data
    elif not isinstance(data, pandas.DataFrame):
      raise RuntimeError("Expected a Session or DataFrame for 'data', got a %r" % tools.fullqual(type(data)))
    # Get the x-axis values
    if self._idx is None:
      x = data.index.to_numpy()
    else:
      if self._idx not in data:
        raise RuntimeError("No column named %r to use as index in the given session/dataframe" % (self._idx,))
      x = data[self._idx].to_numpy()
    # Select semantic: empty list = select all
    if len(cols) == 0:
      cols = data.columns.to_list()
    # For every selection
    axis = None
    for col in cols:
      # Get associated data
      subd = select(data, col)
      # For every selected column
      for scol in subd:
        # Ignore index column
        if self._idx is not None and scol == self._idx:
          continue
        # Ignore error column
        if errs is not None and scol[:-len(errs)] in subd:
          continue
        # Get associated axis (if not done yet)
        if axis is None:
          axis = self._get_ax(col)
        # Pick a new line style and color
        linestyle, color = self._get_line_style(self._cnt if ccnt is None else ccnt)
        # Plot the data (line or error line)
        davg = subd[scol].to_numpy()
        errn = None if errs is None else (scol + errs)
        if errn is not None and errn in data:
          derr = data[errn].to_numpy()
          axis.fill_between(x, davg - derr, davg + derr, facecolor=color, alpha=0.2)
        axis.plot(x, davg, label=scol, linestyle=linestyle, color=color, alpha=lalp)
        # Increase the counter only on success
        self._cnt += 1
      # Reset axis for next iteration
      axis = None
    # Return self for chaining
    return self

  def include_single(self, data, key, col, err=None, lalp=1., ccnt=None):
    """ Add one line with column of the given data frame, can only be done before finalization.
    Args:
      data Session or dataframe holding the column(s) to add
      key  Displayed name (in the key)
      col  Single column name to include
      err  Optional associated error column name
      lalp Line alpha level
      ccnt Color and linestyle number to use
    Returns:
      self
    """
    # Assert not already finalized
    if self._fin:
      raise RuntimeError("Plot is already finalized and cannot include another line")
    # Recover the dataframe if a session was given
    if isinstance(data, Session):
      data = data.data
    elif not isinstance(data, pandas.DataFrame):
      raise RuntimeError("Expected a Session or DataFrame for 'data', got a %r" % tools.fullqual(type(data)))
    # Get the x-axis values
    if self._idx is None:
      x = data.index.to_numpy()
    else:
      if self._idx not in data:
        raise RuntimeError("No column named %r to use as index in the given session/dataframe" % (self._idx,))
      x = data[self._idx].to_numpy()
    # Pick a new line style and color
    linestyle, color = self._get_line_style(self._cnt if ccnt is None else ccnt)
    # Plot the data (line and error line)
    davg = data[col].to_numpy()
    derr = None if err is None else data[err].to_numpy()
    axis = self._get_ax(col)
    if derr is not None:
      axis.fill_between(x, davg - derr, davg + derr, facecolor=color, alpha=0.2)
    axis.plot(x, davg, label=key, linestyle=linestyle, color=color, alpha=lalp)
    # Increase the counter only on success
    self._cnt += 1
    # Return self for chaining
    return self

  def include_vline(self, x, color="black", label=None, ls=None, lw=2):
    """ Draw a vertical line at the given abscissa.
    Args:
      x Abscissa at which to draw the vertical line
    """
    self._ax.axvline(x=x, ls=ls, lw=lw, color=color, label=label)

  def finalize(self, title, xlabel, ylabel, zlabel=None, xmin=None, xmax=None, ymin=None, ymax=None, zmin=None, zmax=None, legend=None):
    """ Finalize the plot, can be done only once and would prevent further inclusion.
    Args:
      title  Plot title
      xlabel Label for the x-axis
      ylabel Label for the y-axis
      zlabel Label for the twin y-axis, if any
      xmin   Minimum for abscissa, if any
      xmax   Maximum for abscissa, if any
      ymin   Minimum for ordinate, if any
      ymax   Maximum for ordinate, if any
      zmin   Minimum for second ordinate, if any
      zmax   Maximum for second ordinate, if any
      legend List of strings (one per 'include', in call order) to use as legend
    Returns:
      self
    """
    # Fast path
    if self._fin:
      return self
    # Plot the legend
    def generator_sum(gen):
      res = None
      while True:
        try:
          val = next(gen)
          if res is None:
            res = val
          else:
            res += val
        except StopIteration:
          return res
    (self._ax if self._tax is None else self._tax).legend(generator_sum(ax.get_legend_handles_labels()[0] for ax in self._axs.values()), generator_sum(ax.get_legend_handles_labels()[1] for ax in self._axs.values()) if legend is None else legend, loc="best")
    # Plot the grid and labels
    self._ax.grid()
    self._ax.set_xlabel(xlabel)
    self._ax.set_ylabel(ylabel)
    self._ax.set_title(title)
    if zlabel is not None:
      if self._tax is None:
        tools.warning("No secondary y-axis found, but its label %r was provided" % (zlabel,))
      else:
        self._tax.set_ylabel(zlabel)
    elif self._tax is not None:
      tools.warning("No label provided for the secondary y-axis; using label %r from the primary" % (ylabel,))
      self._tax.set_ylabel(ylabel)
    self._ax.set_xlim(left=xmin, right=xmax)
    self._ax.set_ylim(bottom=ymin, top=ymax)
    if self._tax is not None:
      self._tax.set_ylim(bottom=zmin, top=zmax)
    # Mark finalized
    self._fin = True
    # Return self for chaining
    return self

  def display(self):
    """ Display the figure, which must have been finalized.
    Returns:
      self
    """
    # Assert already finalized
    if not self._fin:
      raise RuntimeError("Cannot display a plot that has not been finalized yet")
    # Show the plot
    self._fig.show()
    # Return self for chaining
    return self

  def save(self, path, dpi=200, xsize=3, ysize=2):
    """ Save the figure, which must have been finalized.
    Args:
      path  Path of the file to write
      dpi   Output image DPI (very good quality printing is usually 300 DPI)
      xsize Output image x-size (in cm)
      ysize Output image y-size (in cm)
    Returns:
      self
    """
    # Assert already finalized
    if not self._fin:
      raise RuntimeError("Cannot display a plot that has not been finalized yet")
    # Save the figure
    self._fig.set_size_inches(xsize * 2.54, ysize * 2.54)
    self._fig.set_dpi(dpi)
    self._fig.savefig(path)
    # Return self for chaining
    return self

  def close(self):
    """ Explicitly "close" the associated figure (needed by pyplot), the instance cannot be used anymore after the call.
    """
    if self._fig is not None: # The documentation of 'plt.close' does not explicitly specify that multiple calls are allowed on the same 'Figure'
      plt.close(self._fig)
      self._fig = None

class HistPlot:
  """ Histogram plot management class.
  """

  def __init__(self, bins=25):
    """ Number of bins histogram constructor.
    Args:
      bins Number of bins to use
    """
    # Make the subplots
    fig, axs = plt.subplots()
    # Finalize
    self._bins = bins
    self._fin  = False
    self._fig  = fig
    self._ax   = axs

  def __del__(self):
    """ Close the figure on finalization.
    """
    self.close()

  def include(self, data):
    """ Make the histogram from the raw data.
    Args:
      data Given Series or numpy data array
    Returns:
      self
    """
    # Convert 'pandas.Series' to numpy
    if isinstance(data, pandas.Series):
      data = data.to_numpy()
    # Make the histogram
    self._ax.hist(data, bins=self._bins)
    # Return self for chaining
    return self

  def finalize(self, title, xlabel, ylabel, xmin=None, xmax=None, ymin=None, ymax=None):
    """ Finalize the plot, can be done only once and would prevent further inclusion.
    Args:
      title  Plot title
      xlabel Label for the x-axis
      ylabel Label for the y-axis
      xmin   Minimum for abscissa, if any
      xmax   Maximum for abscissa, if any
      ymin   Minimum for ordinate, if any
      ymax   Maximum for ordinate, if any
      legend List of strings (one per 'include', in call order) to use as legend
    Returns:
      self
    """
    # Fast path
    if self._fin:
      return self
    # Plot the grid and labels
    self._ax.grid()
    self._ax.set_xlabel(xlabel)
    self._ax.set_ylabel(ylabel)
    self._ax.set_title(title)
    self._ax.set_xlim(left=xmin, right=xmax)
    self._ax.set_ylim(bottom=ymin, top=ymax)
    # Mark finalized
    self._fin = True
    # Return self for chaining
    return self

  def display(self):
    """ Display the figure, which must have been finalized.
    Returns:
      self
    """
    # Assert already finalized
    if not self._fin:
      raise RuntimeError("Cannot display a plot that has not been finalized yet")
    # Show the plot
    self._fig.show()
    # Return self for chaining
    return self

  def save(self, path, dpi=200, xsize=3, ysize=2):
    """ Save the figure, which must have been finalized.
    Args:
      path  Path of the file to write
      dpi   Output image DPI (very good quality printing is usually 300 DPI)
      xsize Output image x-size (in cm)
      ysize Output image y-size (in cm)
    Returns:
      self
    """
    # Assert already finalized
    if not self._fin:
      raise RuntimeError("Cannot display a plot that has not been finalized yet")
    # Save the figure
    self._fig.set_size_inches(xsize * 2.54, ysize * 2.54)
    self._fig.set_dpi(dpi)
    self._fig.savefig(path)
    # Return self for chaining
    return self

  def close(self):
    """ Explicitly "close" the associated figure (needed by pyplot), the instance cannot be used anymore after the call.
    """
    if self._fig is not None: # The documentation of 'plt.close' does not explicitly specify that multiple calls are allowed on the same 'Figure'
      plt.close(self._fig)
      self._fig = None
