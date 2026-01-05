#!/usr/bin/env python3
"""
Benchmark script for Python 3.14 free-threading performance.

This script measures the performance of key operations with and without
free-threading to quantify the improvements from parallel execution.

Run with:
    python -m autoortho.perftest_314           # Normal mode
    python -X gil=0 -m autoortho.perftest_314  # Free-threading mode (Python 3.14+)
"""
import sys
import os
import time
import statistics
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from autoortho.utils.constants import (
        FREE_THREADING_ENABLED,
        CURRENT_CPU_COUNT,
        is_free_threaded,
    )
except ImportError:
    from utils.constants import (
        FREE_THREADING_ENABLED,
        CURRENT_CPU_COUNT,
        is_free_threaded,
    )

try:
    from autoortho import pydds
    from autoortho.aoimage import AoImage
    PYDDS_AVAILABLE = True
except ImportError:
    try:
        import pydds
        from aoimage import AoImage
        PYDDS_AVAILABLE = True
    except ImportError:
        PYDDS_AVAILABLE = False


def print_header():
    """Print benchmark header with system info."""
    print("=" * 70)
    print("AutoOrtho Python 3.14 Free-Threading Performance Benchmark")
    print("=" * 70)
    print(f"Python version: {sys.version}")
    print(f"CPU cores: {CURRENT_CPU_COUNT}")
    print(f"Free-threading enabled: {FREE_THREADING_ENABLED}")
    if sys.version_info >= (3, 14):
        gil_enabled = getattr(sys, '_is_gil_enabled', lambda: True)()
        print(f"GIL enabled: {gil_enabled}")
    print("=" * 70)
    print()


def benchmark_parallel_execution(num_tasks=20, work_per_task=0.01, workers_list=None):
    """
    Benchmark parallel task execution with different worker counts.
    
    This simulates CPU-bound work to measure the impact of free-threading.
    
    Args:
        num_tasks: Number of tasks to execute
        work_per_task: Simulated work time per task (seconds)
        workers_list: List of worker counts to test
    """
    if workers_list is None:
        workers_list = [1, 2, 4, 8, CURRENT_CPU_COUNT]
    
    print(f"Benchmark: Parallel Execution ({num_tasks} tasks)")
    print("-" * 50)
    
    def cpu_bound_work(task_id):
        """Simulate CPU-bound work."""
        result = 0
        # Do some actual CPU work
        for i in range(100000):
            result += i * i
        return task_id, result
    
    results = {}
    
    for num_workers in workers_list:
        if num_workers > CURRENT_CPU_COUNT * 2:
            continue
            
        times = []
        for _ in range(3):  # Run 3 times for averaging
            start = time.perf_counter()
            
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(cpu_bound_work, i) for i in range(num_tasks)]
                for future in as_completed(futures):
                    future.result()
            
            elapsed = time.perf_counter() - start
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        results[num_workers] = avg_time
        print(f"  {num_workers:2d} workers: {avg_time*1000:.1f}ms (avg of 3 runs)")
    
    # Calculate speedup
    if 1 in results and results[1] > 0:
        baseline = results[1]
        print()
        print("  Speedup vs single-threaded:")
        for workers, time_taken in sorted(results.items()):
            if workers > 1:
                speedup = baseline / time_taken
                print(f"    {workers:2d} workers: {speedup:.2f}x")
    
    print()
    return results


def benchmark_dds_compression(image_sizes=None, iterations=3):
    """
    Benchmark DDS compression performance.
    
    Args:
        image_sizes: List of (width, height) tuples to test
        iterations: Number of iterations per size
    """
    if not PYDDS_AVAILABLE:
        print("Benchmark: DDS Compression - SKIPPED (pydds not available)")
        print()
        return None
    
    if image_sizes is None:
        image_sizes = [(256, 256), (512, 512), (1024, 1024)]
    
    print("Benchmark: DDS Compression")
    print("-" * 50)
    
    results = {}
    
    for width, height in image_sizes:
        try:
            # Create a test image
            img = AoImage.new('RGBA', (width, height), (128, 128, 128))
            if img is None:
                print(f"  {width}x{height}: SKIPPED (could not create image)")
                continue
            
            times = []
            for _ in range(iterations):
                dds = pydds.DDS(width, height, ispc=True, dxt_format="BC1")
                
                start = time.perf_counter()
                dds.gen_mipmaps(img, startmipmap=0, maxmipmaps=4)
                elapsed = time.perf_counter() - start
                
                times.append(elapsed)
            
            avg_time = statistics.mean(times)
            results[(width, height)] = avg_time
            print(f"  {width}x{height}: {avg_time*1000:.1f}ms (avg of {iterations} runs)")
            
            # Cleanup
            img.close()
            
        except Exception as e:
            print(f"  {width}x{height}: ERROR - {e}")
    
    print()
    return results


def benchmark_parallel_mipmap_generation(size=1024, iterations=3):
    """
    Benchmark sequential vs parallel mipmap generation.
    
    Args:
        size: Image size (width=height)
        iterations: Number of iterations
    """
    if not PYDDS_AVAILABLE:
        print("Benchmark: Parallel Mipmap Generation - SKIPPED (pydds not available)")
        print()
        return None
    
    print("Benchmark: Sequential vs Parallel Mipmap Generation")
    print("-" * 50)
    
    try:
        # Create a test image
        img = AoImage.new('RGBA', (size, size), (100, 150, 100))
        if img is None:
            print("  SKIPPED (could not create image)")
            print()
            return None
        
        results = {}
        
        # Sequential
        seq_times = []
        for _ in range(iterations):
            dds = pydds.DDS(size, size, ispc=True, dxt_format="BC1")
            start = time.perf_counter()
            dds.gen_mipmaps(img, startmipmap=0, maxmipmaps=8)
            elapsed = time.perf_counter() - start
            seq_times.append(elapsed)
        
        seq_avg = statistics.mean(seq_times)
        results['sequential'] = seq_avg
        print(f"  Sequential: {seq_avg*1000:.1f}ms (avg of {iterations} runs)")
        
        # Parallel (if free-threading enabled)
        if FREE_THREADING_ENABLED:
            par_times = []
            for _ in range(iterations):
                dds = pydds.DDS(size, size, ispc=True, dxt_format="BC1")
                start = time.perf_counter()
                dds.gen_mipmaps_parallel(img, startmipmap=0, maxmipmaps=8)
                elapsed = time.perf_counter() - start
                par_times.append(elapsed)
            
            par_avg = statistics.mean(par_times)
            results['parallel'] = par_avg
            speedup = seq_avg / par_avg if par_avg > 0 else 0
            print(f"  Parallel:   {par_avg*1000:.1f}ms (avg of {iterations} runs)")
            print(f"  Speedup:    {speedup:.2f}x")
        else:
            print("  Parallel:   SKIPPED (free-threading not enabled)")
        
        # Cleanup
        img.close()
        
    except Exception as e:
        print(f"  ERROR: {e}")
        results = None
    
    print()
    return results


def benchmark_cache_io(num_files=50, file_size_kb=256, workers_list=None):
    """
    Benchmark parallel cache I/O operations.
    
    Args:
        num_files: Number of files to write/read
        file_size_kb: Size of each file in KB
        workers_list: List of worker counts to test
    """
    import tempfile
    import shutil
    
    if workers_list is None:
        workers_list = [1, 2, 4, 8]
    
    print(f"Benchmark: Parallel Cache I/O ({num_files} files, {file_size_kb}KB each)")
    print("-" * 50)
    
    # Create temp directory
    temp_dir = tempfile.mkdtemp(prefix="ao_bench_")
    
    try:
        # Generate test data
        test_data = os.urandom(file_size_kb * 1024)
        
        results = {}
        
        for num_workers in workers_list:
            if num_workers > CURRENT_CPU_COUNT * 2:
                continue
            
            # Clean up from previous iteration
            for f in os.listdir(temp_dir):
                os.remove(os.path.join(temp_dir, f))
            
            def write_file(file_idx):
                path = os.path.join(temp_dir, f"cache_{file_idx}.dat")
                with open(path, 'wb') as f:
                    f.write(test_data)
                return path
            
            # Benchmark writes
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                list(executor.map(write_file, range(num_files)))
            write_time = time.perf_counter() - start
            
            def read_file(file_idx):
                path = os.path.join(temp_dir, f"cache_{file_idx}.dat")
                with open(path, 'rb') as f:
                    return f.read()
            
            # Benchmark reads
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                list(executor.map(read_file, range(num_files)))
            read_time = time.perf_counter() - start
            
            results[num_workers] = {'write': write_time, 'read': read_time}
            print(f"  {num_workers:2d} workers: write={write_time*1000:.1f}ms, read={read_time*1000:.1f}ms")
        
    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    print()
    return results


def run_all_benchmarks():
    """Run all benchmarks and summarize results."""
    print_header()
    
    print("Running benchmarks...\n")
    
    # Run benchmarks
    parallel_results = benchmark_parallel_execution()
    dds_results = benchmark_dds_compression()
    mipmap_results = benchmark_parallel_mipmap_generation()
    cache_results = benchmark_cache_io()
    
    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if FREE_THREADING_ENABLED:
        print("Free-threading is ENABLED - parallel operations should show speedup")
        print()
        
        if parallel_results and 1 in parallel_results and CURRENT_CPU_COUNT in parallel_results:
            speedup = parallel_results[1] / parallel_results[CURRENT_CPU_COUNT]
            print(f"  CPU-bound parallel execution speedup: {speedup:.2f}x")
        
        if mipmap_results and 'sequential' in mipmap_results and 'parallel' in mipmap_results:
            speedup = mipmap_results['sequential'] / mipmap_results['parallel']
            print(f"  Mipmap generation speedup: {speedup:.2f}x")
    else:
        print("Free-threading is DISABLED - running in GIL mode")
        print()
        print("To enable free-threading (Python 3.14+):")
        print("  python -X gil=0 -m autoortho.perftest_314")
        print("  or set PYTHON_GIL=0 environment variable")
    
    print()
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark Python 3.14 free-threading performance"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run quick benchmarks with fewer iterations"
    )
    parser.add_argument(
        "--parallel-only",
        action="store_true",
        help="Only run parallel execution benchmark"
    )
    parser.add_argument(
        "--dds-only",
        action="store_true",
        help="Only run DDS compression benchmark"
    )
    
    args = parser.parse_args()
    
    print_header()
    
    if args.parallel_only:
        benchmark_parallel_execution()
    elif args.dds_only:
        if PYDDS_AVAILABLE:
            benchmark_dds_compression()
            benchmark_parallel_mipmap_generation()
        else:
            print("DDS benchmarks require pydds module")
    else:
        run_all_benchmarks()


if __name__ == "__main__":
    main()

