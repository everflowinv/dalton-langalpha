import * as React from "react"
import * as ToastPrimitives from "@radix-ui/react-toast"
import { cva, type VariantProps } from "class-variance-authority"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"
import { aboveDialogs } from "@/hooks/useDialogA11y"

const ToastProvider = ToastPrimitives.Provider

function ToastViewport({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Viewport>) {
  // Above every modal layer, so a toast raised from inside a dialog is not
  // dimmed under its backdrop. The empty viewport still spans its padding, so it
  // passes pointer events through and only the toasts themselves take them.
  // Being above them is also why an open dialog lets a toast keep focus.
  //
  // `--toast-lift` is written by `useToastInset` for any element floating at
  // the bottom of the page, so a toast stacks above its buttons rather than
  // over them. Unset everywhere else, which leaves the edge at 0.
  return (
    <ToastPrimitives.Viewport
      {...aboveDialogs}
      className={cn(
        "pointer-events-none fixed top-0 z-1050 flex max-h-screen w-full flex-col-reverse p-4 sm:bottom-[var(--toast-lift,0px)] sm:right-0 sm:top-auto sm:flex-col sm:transition-[bottom] motion-reduce:transition-none md:max-w-[420px]",
        className
      )}
      {...props}
    />
  )
}

// The gap sits before every child after the first, not after every child but
// the last (space-x-4): the close button is the last child and is absolutely
// placed, so space-x would leave its gap on the action beside it. Positioning
// goes through `transform` because the enter and exit keyframes animate
// `transform`, which would stack on the separate `translate` property that
// translate-x-* sets instead of replacing it.
const toastVariants = cva(
  "group pointer-events-auto relative flex w-full items-center justify-between [&>:not([hidden])~:not([hidden])]:ml-4 overflow-hidden rounded-md border p-6 pr-8 shadow-lg transition-all data-[swipe=cancel]:[transform:translateX(0)] data-[swipe=end]:[transform:translateX(var(--radix-toast-swipe-end-x))] data-[swipe=move]:[transform:translateX(var(--radix-toast-swipe-move-x))] data-[swipe=move]:transition-none toast-slide",
  {
    variants: {
      variant: {
        default: "border bg-background text-foreground",
        destructive:
          "destructive group border-red-500 bg-red-600 text-white",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Toast({
  className,
  variant,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Root> & VariantProps<typeof toastVariants>) {
  return (
    <ToastPrimitives.Root
      className={cn(toastVariants({ variant }), className)}
      {...props}
    />
  )
}

function ToastAction({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Action>) {
  return (
    <ToastPrimitives.Action
      className={cn(
        "inline-flex h-8 shrink-0 items-center justify-center rounded-md border bg-transparent px-3 text-sm font-medium ring-offset-background transition-colors hover:bg-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 group-[.destructive]:border-muted/40 hover:group-[.destructive]:border-destructive/30 hover:group-[.destructive]:bg-destructive hover:group-[.destructive]:text-destructive-foreground focus-visible:group-[.destructive]:ring-destructive",
        className
      )}
      {...props}
    />
  )
}

// Revealed by hovering the toast, which a touch screen cannot do: there, and
// while anything in the toast holds focus, it is simply shown.
function ToastClose({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Close>) {
  return (
    <ToastPrimitives.Close
      className={cn(
        "absolute right-2 top-2 rounded-md p-1 text-foreground/50 opacity-0 transition-opacity hover:text-foreground focus:opacity-100 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100 group-[.destructive]:text-red-300 hover:group-[.destructive]:text-red-50 focus-visible:group-[.destructive]:ring-red-400",
        className
      )}
      toast-close=""
      {...props}
    >
      <X className="h-4 w-4" />
    </ToastPrimitives.Close>
  )
}

function ToastTitle({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Title>) {
  return (
    <ToastPrimitives.Title
      className={cn("text-sm font-semibold", className)}
      {...props}
    />
  )
}

function ToastDescription({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Description>) {
  return (
    <ToastPrimitives.Description
      className={cn("text-sm opacity-90", className)}
      {...props}
    />
  )
}

export {
  ToastProvider,
  ToastViewport,
  Toast,
  ToastTitle,
  ToastDescription,
  ToastClose,
  ToastAction,
}
